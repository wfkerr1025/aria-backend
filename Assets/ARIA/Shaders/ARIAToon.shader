// Assets/ARIA/Shaders/ARIAToon.shader
//
// Anime shading for ARIA's characters, in URP: flat colour in two tones
// with a crisp edge between them, and an ink outline.
//
// Why it exists: a character made from a drawing and lit by URP Lit came
// out a glossy statue -- smooth gradients, highlights like plastic -- when
// the drawing it came from is flat colour and ink (2026-09-28, a man in a
// coat drawn in the manhwa style). Shading in tones and outlining is what
// makes a model read as that kind of drawing.
//
//   Base Map      the baked colour (the drawing painted back on)
//   Shade         how dark the shaded tone is, and where the edge falls
//   Ambient       how much of the sky still reaches the shaded side --
//                 shadows in a drawing are a colour, never black
//   Light         how far the scene's light may recolour and darken the
//                 drawing: a low sun behind him turned a drawn character
//                 nearly black; at 0 it looks as drawn whatever the sky
//   Rim           a thin light edge on the side away from the camera
//   Outline       width in screen pixels (it does not thin with distance
//                 until it is far off), and its colour
Shader "ARIA/Toon"
{
    Properties
    {
        _BaseMap ("Base Map", 2D) = "white" {}
        _BaseColor ("Base Color", Color) = (1, 1, 1, 1)
        _ShadeStrength ("Shade Strength", Range(0, 1)) = 0.3
        _ShadeThreshold ("Shade Threshold", Range(-1, 1)) = 0.05
        _ShadeSoftness ("Shade Softness", Range(0.001, 0.5)) = 0.04
        _AmbientStrength ("Ambient Strength", Range(0, 1)) = 0.2
        _LightInfluence ("Light Influence", Range(0, 1)) = 0.35
        _RimStrength ("Rim Strength", Range(0, 1)) = 0.15
        _RimWidth ("Rim Width", Range(0.01, 1)) = 0.25
        _OutlineWidth ("Outline Width (px)", Range(0, 8)) = 1.6
        _OutlineColor ("Outline Color", Color) = (0.06, 0.05, 0.08, 1)
    }

    SubShader
    {
        Tags { "RenderType" = "Opaque" "RenderPipeline" = "UniversalPipeline" "Queue" = "Geometry" }

        HLSLINCLUDE
        #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"

        CBUFFER_START(UnityPerMaterial)
            float4 _BaseMap_ST;
            half4 _BaseColor;
            half _ShadeStrength;
            half _ShadeThreshold;
            half _ShadeSoftness;
            half _AmbientStrength;
            half _LightInfluence;
            half _RimStrength;
            half _RimWidth;
            half _OutlineWidth;
            half4 _OutlineColor;
        CBUFFER_END

        TEXTURE2D(_BaseMap);
        SAMPLER(sampler_BaseMap);
        ENDHLSL

        Pass
        {
            Name "ForwardToon"
            Tags { "LightMode" = "UniversalForward" }
            // Both sides: a generated mesh has faces turned inward that no
            // fix finds every one of, and culled they were holes onto the
            // dark lining behind -- black patches on a white shirt, on a
            // model that drew clean in Blender, which draws both sides.
            Cull Off

            HLSLPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #pragma multi_compile _ _MAIN_LIGHT_SHADOWS _MAIN_LIGHT_SHADOWS_CASCADE _MAIN_LIGHT_SHADOWS_SCREEN
            #pragma multi_compile_fragment _ _SHADOWS_SOFT
            #pragma multi_compile_fog
            #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Lighting.hlsl"

            struct Attributes
            {
                float4 positionOS : POSITION;
                float3 normalOS : NORMAL;
                float2 uv : TEXCOORD0;
            };

            struct Varyings
            {
                float4 positionCS : SV_POSITION;
                float2 uv : TEXCOORD0;
                float3 normalWS : TEXCOORD1;
                float3 positionWS : TEXCOORD2;
                float fog : TEXCOORD3;
            };

            Varyings vert(Attributes input)
            {
                Varyings output;
                VertexPositionInputs position = GetVertexPositionInputs(input.positionOS.xyz);
                output.positionCS = position.positionCS;
                output.positionWS = position.positionWS;
                output.normalWS = TransformObjectToWorldNormal(input.normalOS);
                output.uv = TRANSFORM_TEX(input.uv, _BaseMap);
                output.fog = ComputeFogFactor(position.positionCS.z);
                return output;
            }

            half4 frag(Varyings input, FRONT_FACE_TYPE face : FRONT_FACE_SEMANTIC) : SV_Target
            {
                half4 albedo = SAMPLE_TEXTURE2D(_BaseMap, sampler_BaseMap, input.uv) * _BaseColor;
                // A generated mesh is not always one-sided: a face seen from its
                // back is shaded as its front would be, not black.
                float3 normal = normalize(input.normalWS) * IS_FRONT_VFACE(face, 1.0, -1.0);
                Light light = GetMainLight(TransformWorldToShadowCoord(input.positionWS));
                half facing = dot(normal, light.direction) * light.shadowAttenuation;
                half lit = smoothstep(_ShadeThreshold - _ShadeSoftness, _ShadeThreshold + _ShadeSoftness, facing);
                half3 shaded = albedo.rgb * (1.0 - _ShadeStrength);
                half3 tint = lerp(half3(1, 1, 1), light.color, _LightInfluence);
                half3 colour = lerp(shaded, albedo.rgb, lit) * tint;
                colour += albedo.rgb * SampleSH(normal) * _AmbientStrength;
                float3 toCamera = normalize(GetWorldSpaceViewDir(input.positionWS));
                half rim = smoothstep(1.0 - _RimWidth, 1.0, 1.0 - saturate(dot(normal, toCamera)));
                colour += rim * _RimStrength * lit * tint;
                colour = MixFog(colour, input.fog);
                return half4(colour, 1.0);
            }
            ENDHLSL
        }

        // The ink line: the back faces, pushed out along their normals by a
        // fixed number of pixels, drawn in one dark colour.
        Pass
        {
            Name "Outline"
            Tags { "LightMode" = "SRPDefaultUnlit" }
            Cull Front

            HLSLPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #pragma multi_compile_fog

            struct Attributes
            {
                float4 positionOS : POSITION;
                float3 normalOS : NORMAL;
            };

            struct Varyings
            {
                float4 positionCS : SV_POSITION;
                float fog : TEXCOORD0;
            };

            Varyings vert(Attributes input)
            {
                Varyings output;
                float4 clip = TransformObjectToHClip(input.positionOS.xyz);
                float3 normalCS = mul((float3x3)UNITY_MATRIX_VP, TransformObjectToWorldNormal(input.normalOS));
                float2 push = normalize(normalCS.xy + 1e-6);
                // Pixels to clip space: times w keeps the width constant on
                // screen; the cap stops a far character's line swallowing it.
                float2 pixel = 2.0 / _ScreenParams.xy;
                clip.xy += push * pixel * _OutlineWidth * min(clip.w, 8.0);
                output.positionCS = clip;
                output.fog = ComputeFogFactor(clip.z);
                return output;
            }

            half4 frag(Varyings input) : SV_Target
            {
                return half4(MixFog(_OutlineColor.rgb, input.fog), 1.0);
            }
            ENDHLSL
        }

        // Pass names in capitals: UsePass looks them up as Unity stores them,
        // and one it cannot find takes the whole subshader down with it.
        UsePass "Universal Render Pipeline/Lit/SHADOWCASTER"
        UsePass "Universal Render Pipeline/Lit/DEPTHONLY"
    }

    FallBack "Universal Render Pipeline/Unlit"
}
