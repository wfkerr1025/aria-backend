// ARIA -- a character from Blender, set up in Unity by the time it lands.
//
// ARIA's Blender layer writes two files into Assets/ARIA/Characters/<Name>/:
//
//     <Name>.fbx         the mesh, its skeleton (Unity Humanoid bone names)
//                        and its clips as separate takes ("Body_Rig|Walk")
//     <Name>.aria.json   what to do with it: which clips loop, which is idle
//
// and this importer does the rest, whenever Unity imports them -- on the
// next refresh in an open editor, or in a batch run with Unity closed:
//
//     the model imports as a Humanoid, avatar made from this model
//     clips are named after their action ("Walk"), idle/walk loop
//     an AnimatorController: Idle by default, Walk when Speed > 0.1
//     a prefab (a variant of the model) with that controller and avatar
//
// It never touches the scene that is open: everything it instantiates lives
// in a preview scene and is gone before it returns. Rebuilding keeps the
// controller's and prefab's GUIDs, so scenes that use them keep working.
//
// What it did is written to <project>/ARIA/characters/<Name>.json (outside
// Assets, so writing it imports nothing), with pictures of each clip beside
// it when it ran in batch mode -- ARIA reads both back.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Animations;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Animations;
using UnityEngine.Playables;
using UnityEngine.SceneManagement;

namespace ARIA.Characters
{
    [Serializable]
    public class CharacterSidecar
    {
        public string name;
        public string[] loop = new string[0];
        public string idle = "Idle";
        public string walk = "Walk";
        // Other clips to picture on this character (Mixamo and the like), as
        // asset paths -- how retargeting is checked by eye.
        public string[] preview_clips = new string[0];
        // false: a prop -- no skeleton, no avatar, no controller.
        public bool rigged = true;
        public TextureSlot[] textures = new TextureSlot[0];
        // The material those maps belong to; empty means every renderer's.
        public string material = "";
    }

    [Serializable]
    public class TextureSlot
    {
        public string role;    // color | normal | occlusion
        public string path;    // Assets/...
    }

    [Serializable]
    public class ClipReport
    {
        public string name;
        public bool loop;
        public float length;
        public string picture;
        // Where the left foot is (forward, m) in each pictured frame: proof the frames differ.
        public float[] leftFootZ = new float[0];
        public float[] meshFootZ = new float[0];   // the same for the skinned mesh as drawn
        public string pose;                         // root rotation and body line at the first frame
        public int extraCurves;                     // curves on bones that are not the Humanoid's
    }

    [Serializable]
    public class CharacterReport
    {
        public string version = ARIACharacterImporter.Version;
        public string name;
        public string model;
        public string prefab;
        public string controller;
        public bool humanoid;
        public bool avatarValid;
        public bool avatarHuman;
        public int humanBones;
        public string[] missingBones = new string[0];
        public List<ClipReport> clips = new List<ClipReport>();
        public List<string> materials = new List<string>();    // "name: shader"
        public List<string> renderers = new List<string>();    // "name: Skinned (n bones)" or "name: Mesh"
        public List<string> warnings = new List<string>();
        public string error;
        public string builtAt;
        public bool rigged = true;
        public string material;
        public string[] lods = new string[0];
        public string picture;     // a prop's four views
        public string restArm;     // the left upper arm's direction before any clip
        public string[] humanMap = new string[0];   // Unity body part = our bone
    }

    public class ARIACharacterImporter : AssetPostprocessor
    {
        public const string Version = "1.0.0";
        const string SidecarSuffix = ".aria.json";

        // The bones Unity will not make a Humanoid without.
        static readonly string[] RequiredBones = {
            "Hips", "Spine", "Head", "LeftUpperArm", "LeftLowerArm", "LeftHand",
            "RightUpperArm", "RightLowerArm", "RightHand", "LeftUpperLeg", "LeftLowerLeg",
            "LeftFoot", "RightUpperLeg", "RightLowerLeg", "RightFoot" };

        public static string SidecarPathFor(string modelPath)
        {
            return modelPath.Substring(0, modelPath.Length - Path.GetExtension(modelPath).Length) + SidecarSuffix;
        }

        public static CharacterSidecar ReadSidecar(string modelPath)
        {
            string path = SidecarPathFor(modelPath);
            if (!modelPath.EndsWith(".fbx", StringComparison.OrdinalIgnoreCase) || !File.Exists(path))
                return null;
            try
            {
                var sidecar = JsonUtility.FromJson<CharacterSidecar>(File.ReadAllText(path));
                if (string.IsNullOrEmpty(sidecar.name))
                    sidecar.name = Path.GetFileNameWithoutExtension(modelPath);
                return sidecar;
            }
            catch (Exception error)
            {
                Debug.LogWarning("[ARIA] could not read " + path + ": " + error.Message);
                return null;
            }
        }

        static string ShortName(string take)
        {
            int bar = take.LastIndexOf('|');
            return bar >= 0 ? take.Substring(bar + 1) : take;
        }

        void OnPreprocessModel()
        {
            var sidecar = ReadSidecar(assetPath);
            if (sidecar == null)
                return;
            var importer = (ModelImporter)assetImporter;
            if (!sidecar.rigged)
            {
                importer.animationType = ModelImporterAnimationType.None;
                importer.importAnimation = false;
                return;
            }
            importer.animationType = ModelImporterAnimationType.Human;
            importer.avatarSetup = ModelImporterAvatarSetup.CreateFromThisModel;
            importer.importAnimation = true;
            importer.optimizeGameObjects = false;
        }

        void OnPreprocessAnimation()
        {
            var sidecar = ReadSidecar(assetPath);
            if (sidecar == null || !sidecar.rigged)
                return;
            var importer = (ModelImporter)assetImporter;
            var loops = new HashSet<string>((sidecar.loop ?? new string[0]).Select(l => l.ToLowerInvariant()));
            // "!Rest" is there only to be the first take -- the pose Unity reads
            // the model in (see export_fbx on ARIA's side). Not a clip.
            var clips = importer.defaultClipAnimations
                .Where(c => !ShortName(c.takeName).StartsWith("!")).ToArray();
            var before = importer.clipAnimations ?? new ModelImporterClipAnimation[0];
            foreach (var clip in clips)
            {
                // Keep a mask set on an earlier import (see MaskExtraBones): rebuilt
                // from the defaults, the clips lost it and dropped the cape again.
                var kept = before.FirstOrDefault(b => b.takeName == clip.takeName);
                if (kept != null && kept.maskType == ClipAnimationMaskType.CreateFromThisModel)
                {
                    var mask = new AvatarMask();
                    kept.ConfigureMaskFromClip(ref mask);
                    clip.maskType = ClipAnimationMaskType.CreateFromThisModel;
                    clip.ConfigureClipFromMask(mask);
                }
                clip.name = ShortName(clip.takeName);
                clip.loopTime = loops.Contains(clip.name.ToLowerInvariant());
                clip.loopPose = clip.loopTime;
                // In place: ARIA's clips walk on the spot, and a root that
                // drifted would carry the character off its capsule.
                clip.lockRootRotation = true;
                clip.lockRootHeightY = true;
                clip.lockRootPositionXZ = true;
                clip.keepOriginalOrientation = true;
                clip.keepOriginalPositionY = true;
                clip.keepOriginalPositionXZ = true;
            }
            importer.clipAnimations = clips;
        }

        static void OnPostprocessAllAssets(string[] imported, string[] deleted, string[] moved, string[] movedFrom)
        {
            if (Application.isBatchMode)
                return;                               // ImportPending builds them itself
            var models = new HashSet<string>();
            foreach (string path in imported)
            {
                if (path.EndsWith(SidecarSuffix, StringComparison.OrdinalIgnoreCase))
                {
                    string stem = path.Substring(0, path.Length - SidecarSuffix.Length);
                    if (File.Exists(stem + ".fbx")) models.Add(stem + ".fbx");
                }
                else if (ReadSidecar(path) != null)
                {
                    models.Add(path);
                }
            }
            if (models.Count == 0)
                return;
            // Assets are not to be made while an import is running.
            EditorApplication.delayCall += () =>
            {
                foreach (string model in models)
                    Build(model, render: false);
            };
        }

        // Unity -batchmode -projectPath <p> -executeMethod ARIA.Characters.ARIACharacterImporter.ImportPending -quit
        public static void ImportPending()
        {
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
            foreach (string sidecar in Directory.GetFiles("Assets", "*" + SidecarSuffix, SearchOption.AllDirectories))
            {
                string model = sidecar.Replace('\\', '/');
                model = model.Substring(0, model.Length - SidecarSuffix.Length) + ".fbx";
                if (!File.Exists(model))
                    continue;
                // Only what changed since it was last built: a project with ten
                // characters should not rebuild and re-picture all ten for one.
                string name = Path.GetFileNameWithoutExtension(model);
                var sidecarData = ReadSidecar(model);
                if (sidecarData != null) name = sidecarData.name;
                string report = Path.Combine(ReportFolder(), name + ".json");
                if (File.Exists(report) && File.GetLastWriteTimeUtc(report) > File.GetLastWriteTimeUtc(model)
                    && File.GetLastWriteTimeUtc(report) > File.GetLastWriteTimeUtc(sidecar))
                    continue;
                // The sidecar may have arrived after the model was imported
                // without it: import again, now that it is there.
                AssetDatabase.ImportAsset(model, ImportAssetOptions.ForceUpdate | ImportAssetOptions.ForceSynchronousImport);
                Build(model, render: true);
            }
        }

        [MenuItem("ARIA/Characters/Rebuild All")]
        public static void RebuildAll()
        {
            foreach (string sidecar in Directory.GetFiles("Assets", "*" + SidecarSuffix, SearchOption.AllDirectories))
            {
                string model = sidecar.Replace('\\', '/');
                model = model.Substring(0, model.Length - SidecarSuffix.Length) + ".fbx";
                if (File.Exists(model))
                {
                    AssetDatabase.ImportAsset(model, ImportAssetOptions.ForceUpdate);
                    Build(model, render: false);
                }
            }
        }

        public static CharacterReport Build(string modelPath, bool render)
        {
            var sidecar = ReadSidecar(modelPath);
            var report = new CharacterReport
            {
                name = sidecar != null ? sidecar.name : Path.GetFileNameWithoutExtension(modelPath),
                model = modelPath,
                builtAt = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
            };
            try
            {
                BuildInto(report, sidecar, modelPath, render);
            }
            catch (Exception error)
            {
                report.error = error.GetType().Name + ": " + error.Message;
                Debug.LogError("[ARIA] " + report.name + ": " + error);
            }
            Directory.CreateDirectory(ReportFolder());
            File.WriteAllText(Path.Combine(ReportFolder(), report.name + ".json"), JsonUtility.ToJson(report, true));
            Debug.Log("[ARIA] character " + report.name + (report.error == null ? " built" : " FAILED: " + report.error));
            return report;
        }

        static string ReportFolder()
        {
            return Path.GetFullPath(Path.Combine(Application.dataPath, "..", "ARIA", "characters"));
        }

        static void BuildInto(CharacterReport report, CharacterSidecar sidecar, string modelPath, bool render)
        {
            if (sidecar == null)
                throw new InvalidOperationException("no " + SidecarPathFor(modelPath) + " beside the model");
            var importer = (ModelImporter)AssetImporter.GetAtPath(modelPath);
            var model = AssetDatabase.LoadAssetAtPath<GameObject>(modelPath);
            if (importer == null || model == null)
                throw new InvalidOperationException(modelPath + " has not imported as a model");
            report.rigged = sidecar.rigged;
            report.humanoid = importer.animationType == ModelImporterAnimationType.Human;
            string folder = Path.GetDirectoryName(modelPath).Replace('\\', '/');

            if (sidecar.rigged && MapHumanoid(importer, model))
            {
                importer.SaveAndReimport();
                model = AssetDatabase.LoadAssetAtPath<GameObject>(modelPath);
            }
            if (sidecar.rigged && MaskExtraBones(importer, model))
            {
                importer.SaveAndReimport();
                model = AssetDatabase.LoadAssetAtPath<GameObject>(modelPath);
            }
            var assets = AssetDatabase.LoadAllAssetsAtPath(modelPath);
            Avatar avatar = null;
            AnimatorController controller = null;
            var clips = new List<AnimationClip>();
            if (sidecar.rigged)
            {
                avatar = assets.OfType<Avatar>().FirstOrDefault();
                report.avatarValid = avatar != null && avatar.isValid;
                report.avatarHuman = avatar != null && avatar.isHuman;
                // HumanTrait names ("LeftUpperArm", or "Left Upper Arm" in places): compared without spaces.
                var description = avatar != null ? avatar.humanDescription : importer.humanDescription;
                var mapped = new HashSet<string>((description.human ?? new HumanBone[0]).Select(h => h.humanName.Replace(" ", "")));
                report.humanBones = mapped.Count;
                report.humanMap = (description.human ?? new HumanBone[0])
                    .Select(h => h.humanName.Replace(" ", "") + "=" + h.boneName).OrderBy(x => x).ToArray();
                report.missingBones = RequiredBones.Where(b => !mapped.Contains(b)).ToArray();
                if (!report.avatarHuman)
                    report.warnings.Add("the avatar is not a valid Humanoid -- clips from other characters will not play on it");
                clips = assets.OfType<AnimationClip>().Where(c => !c.name.StartsWith("__preview__")).ToList();
                string controllerPath = folder + "/" + sidecar.name + ".controller";
                controller = BuildController(controllerPath, clips, sidecar, report);
                report.controller = controllerPath;
            }
            var material = BuildMaterial(folder, sidecar, report);

            string prefabPath = folder + "/" + sidecar.name + ".prefab";
            Scene stage = EditorSceneManager.NewPreviewScene();
            try
            {
                var instance = (GameObject)PrefabUtility.InstantiatePrefab(model, stage);
                instance.name = sidecar.name;
                Animator animator = null;
                if (sidecar.rigged)
                {
                    animator = instance.GetComponent<Animator>() ?? instance.AddComponent<Animator>();
                    animator.runtimeAnimatorController = controller;
                    animator.avatar = avatar;
                    animator.applyRootMotion = false;
                }
                if (material != null)
                    foreach (var r in instance.GetComponentsInChildren<Renderer>(true))
                    {
                        if (string.IsNullOrEmpty(sidecar.material))
                        {
                            r.sharedMaterials = Enumerable.Repeat(material, Math.Max(1, r.sharedMaterials.Length)).ToArray();
                            continue;
                        }
                        var slots = r.sharedMaterials;
                        for (int m = 0; m < slots.Length; m++)
                            if (slots[m] != null && slots[m].name == sidecar.material)
                                slots[m] = material;
                        r.sharedMaterials = slots;
                    }
                var group = instance.GetComponentInChildren<LODGroup>();
                if (group != null)
                    report.lods = group.GetLODs().Select((l, i) => "LOD" + i + " " + string.Join("+",
                        l.renderers.Where(r => r != null).Select(r => r.name)) + " down to " +
                        Mathf.RoundToInt(l.screenRelativeTransitionHeight * 100) + "% of the screen").ToArray();
                PrefabUtility.SaveAsPrefabAsset(instance, prefabPath);
                report.prefab = prefabPath;
                foreach (var r in instance.GetComponentsInChildren<Renderer>(true))
                {
                    var skinned = r as SkinnedMeshRenderer;
                    report.renderers.Add(r.name + ": " + (skinned != null
                        ? "Skinned (" + skinned.bones.Count(b => b != null) + " bones, root " + (skinned.rootBone ? skinned.rootBone.name : "none") + ")"
                        : "Mesh"));
                }
                foreach (var used in instance.GetComponentsInChildren<Renderer>()
                             .SelectMany(r => r.sharedMaterials).Where(m => m != null).Distinct())
                {
                    string shader = used.shader != null ? used.shader.name : "none";
                    report.materials.Add(used.name + ": " + shader);
                    if (shader.StartsWith("Hidden/InternalErrorShader") || !used.shader.isSupported)
                        report.warnings.Add(used.name + " has a shader this project cannot draw (" + shader
                                            + ") -- it shows magenta");
                }

                foreach (var clip in clips)
                {
                    var settings = AnimationUtility.GetAnimationClipSettings(clip);
                    report.clips.Add(new ClipReport
                    {
                        name = clip.name, loop = settings.loopTime, length = clip.length,
                        // Bones that are not the Humanoid's -- a cape's -- ride along as
                        // ordinary curves; how many made it into the clip.
                        extraCurves = AnimationUtility.GetCurveBindings(clip).Count(b => b.path.Length > 0),
                    });
                }
                if (render && !sidecar.rigged)
                {
                    report.picture = Picture(stage, instance, null, null, sidecar.name, null);
                }
                else if (render)
                {
                    // At rest first, no clip: what the skin does on its own, apart
                    // from anything retargeting does to it.
                    var upperArm = animator.isHuman ? animator.GetBoneTransform(HumanBodyBones.LeftUpperArm) : null;
                    var lowerArm = animator.isHuman ? animator.GetBoneTransform(HumanBodyBones.LeftLowerArm) : null;
                    if (upperArm != null && lowerArm != null)
                        report.restArm = (lowerArm.position - upperArm.position).normalized.ToString("F2");
                    report.picture = Picture(stage, instance, null, null, sidecar.name, null);
                    var pictured = new List<AnimationClip>(clips);
                    foreach (string extra in sidecar.preview_clips ?? new string[0])
                    {
                        var found = AssetDatabase.LoadAllAssetsAtPath(extra).OfType<AnimationClip>()
                            .FirstOrDefault(c => !c.name.StartsWith("__preview__"));
                        if (found == null) { report.warnings.Add("no clip in " + extra); continue; }
                        pictured.Add(found);
                        report.clips.Add(new ClipReport { name = found.name, loop = false, length = found.length });
                    }
                    foreach (var clip in pictured)
                    {
                        var entry = report.clips.First(c => c.name == clip.name && c.picture == null);
                        entry.picture = Picture(stage, instance, animator, clip, sidecar.name, entry);
                    }
                }
            }
            finally
            {
                EditorSceneManager.ClosePreviewScene(stage);
            }
            AssetDatabase.SaveAssets();
        }

        // A material from the baked maps -- URP Lit when the project draws with
        // URP, Standard otherwise. Kept at the same path, so its GUID survives.
        static Material BuildMaterial(string folder, CharacterSidecar sidecar, CharacterReport report)
        {
            var maps = new Dictionary<string, Texture2D>();
            foreach (var slot in sidecar.textures ?? new TextureSlot[0])
                if (slot != null && !string.IsNullOrEmpty(slot.path) && !string.IsNullOrEmpty(slot.role))
                    maps[slot.role] = AssetDatabase.LoadAssetAtPath<Texture2D>(slot.path);
            if (maps.Count == 0)
                return null;
            bool urp = UnityEngine.Rendering.GraphicsSettings.currentRenderPipeline != null;
            var shader = Shader.Find(urp ? "Universal Render Pipeline/Lit" : "Standard");
            string path = folder + "/" + sidecar.name + ".mat";
            var material = AssetDatabase.LoadAssetAtPath<Material>(path);
            if (material == null)
            {
                material = new Material(shader);
                AssetDatabase.CreateAsset(material, path);
            }
            material.shader = shader;
            Texture2D map;
            if (maps.TryGetValue("color", out map) && map != null)
                material.SetTexture(urp ? "_BaseMap" : "_MainTex", map);
            if (maps.TryGetValue("normal", out map) && map != null)
            {
                material.SetTexture("_BumpMap", map);
                material.EnableKeyword("_NORMALMAP");
            }
            if (maps.TryGetValue("occlusion", out map) && map != null)
            {
                material.SetTexture("_OcclusionMap", map);
                material.EnableKeyword("_OCCLUSIONMAP");
            }
            material.SetFloat(urp ? "_Smoothness" : "_Glossiness", 0.35f);
            foreach (var missing in maps.Where(m => m.Value == null))
                report.warnings.Add("the " + missing.Key + " map did not import");
            EditorUtility.SetDirty(material);
            report.material = path;
            return material;
        }

        // The maps the sidecars beside this texture say it is: a normal map
        // must import as one, and AO holds data, not colour.
        void OnPreprocessTexture()
        {
            string folder = Path.GetDirectoryName(assetPath);
            if (string.IsNullOrEmpty(folder) || !Directory.Exists(folder))
                return;
            foreach (string file in Directory.GetFiles(folder, "*" + SidecarSuffix))
            {
                CharacterSidecar sidecar;
                try { sidecar = JsonUtility.FromJson<CharacterSidecar>(File.ReadAllText(file)); }
                catch (Exception) { continue; }
                var slot = (sidecar.textures ?? new TextureSlot[0]).FirstOrDefault(t => t != null && string.Equals(
                    t.path, assetPath.Replace('\\', '/'), StringComparison.OrdinalIgnoreCase));
                if (slot == null)
                    continue;
                var importer = (TextureImporter)assetImporter;
                if (slot.role == "normal")
                    importer.textureType = TextureImporterType.NormalMap;
                else if (slot.role == "occlusion")
                    importer.sRGBTexture = false;
                return;
            }
        }

        // The Humanoid mapping, written rather than guessed. ARIA's bones carry
        // Unity's own names, so each body part is the bone of that name, and
        // the reference pose is the model as it stands (its first take is the
        // rest -- see export_fbx). Two things this replaces, both measured:
        // Unity's auto-mapper lost a game-ready character's LeftHand, and the
        // description Unity keeps in the .meta held on to a stride from an
        // earlier import, tilting every clip 65 degrees. True when it changed.
        static bool MapHumanoid(ModelImporter importer, GameObject model)
        {
            var named = new Dictionary<string, Transform>();
            foreach (var t in model.GetComponentsInChildren<Transform>(true))
                if (!named.ContainsKey(t.name)) named[t.name] = t;
            var human = new List<HumanBone>();
            foreach (string trait in HumanTrait.BoneName)
            {
                string bone = trait.Replace(" ", "");
                if (named.ContainsKey(bone))
                {
                    var entry = new HumanBone { humanName = trait, boneName = bone };
                    entry.limit.useDefaultValues = true;
                    human.Add(entry);
                }
            }
            if (RequiredBones.Any(r => !named.ContainsKey(r)))
                return false;                          // not ARIA's naming: leave it to Unity
            var skeleton = model.GetComponentsInChildren<Transform>(true).Select(t => new SkeletonBone
            {
                name = t.name, position = t.localPosition, rotation = t.localRotation, scale = t.localScale,
            }).ToArray();

            var current = importer.humanDescription;
            bool same = current.human != null && current.skeleton != null
                && current.human.Length == human.Count
                && human.All(h => current.human.Any(c => c.humanName == h.humanName && c.boneName == h.boneName))
                && current.skeleton.Length == skeleton.Length
                && skeleton.All(b => current.skeleton.Any(c => c.name == b.name
                    && Quaternion.Angle(c.rotation, b.rotation) < 0.5f && (c.position - b.position).magnitude < 0.001f));
            if (same)
                return false;
            var description = current;
            description.human = human.ToArray();
            description.skeleton = skeleton;
            if (description.armStretch == 0f && description.legStretch == 0f)
            {
                description.upperArmTwist = 0.5f;
                description.lowerArmTwist = 0.5f;
                description.upperLegTwist = 0.5f;
                description.lowerLegTwist = 0.5f;
                description.armStretch = 0.05f;
                description.legStretch = 0.05f;
                description.feetSpacing = 0f;
            }
            importer.humanDescription = description;
            return true;
        }

        // A Humanoid clip keeps curves for bones that are not the Humanoid's -- a
        // cape's -- only when its mask names them. Without one, the cape's baked
        // cloth was dropped on import: 0 extra curves in every clip (measured).
        // True when a clip's mask had to change.
        static bool MaskExtraBones(ModelImporter importer, GameObject model)
        {
            var human = new HashSet<string>(importer.humanDescription.human.Select(h => h.boneName));
            var extra = model.GetComponentsInChildren<Transform>(true)
                .Where(t => t != model.transform && !human.Contains(t.name) && t.GetComponent<Renderer>() == null)
                .Where(t => t.GetComponentsInChildren<Transform>(true).Length > 0 && t.parent != model.transform)
                .ToList();
            if (extra.Count == 0)
                return false;
            var mask = new AvatarMask();
            mask.AddTransformPath(model.transform, true);
            var clips = importer.clipAnimations;
            if (clips == null || clips.Length == 0)
                return false;
            bool changed = false;
            foreach (var clip in clips)
            {
                var current = new AvatarMask();
                if (clip.maskType == ClipAnimationMaskType.CreateFromThisModel)
                    clip.ConfigureMaskFromClip(ref current);
                if (clip.maskType == ClipAnimationMaskType.CreateFromThisModel
                    && current.transformCount == mask.transformCount)
                    continue;
                clip.maskType = ClipAnimationMaskType.CreateFromThisModel;
                clip.ConfigureClipFromMask(mask);
                changed = true;
            }
            if (changed)
                importer.clipAnimations = clips;
            return changed;
        }

        static AnimatorController BuildController(string path, List<AnimationClip> clips, CharacterSidecar sidecar,
                                                  CharacterReport report)
        {
            var controller = AssetDatabase.LoadAssetAtPath<AnimatorController>(path);
            if (controller == null)
            {
                controller = AnimatorController.CreateAnimatorControllerAtPath(path);
            }
            else
            {
                // Rebuilt in place, so the GUID -- and every scene that uses it -- survives.
                foreach (var parameter in controller.parameters.ToArray())
                    controller.RemoveParameter(parameter);
                var old = controller.layers[0].stateMachine;
                foreach (var child in old.states.ToArray())
                    old.RemoveState(child.state);
            }
            controller.AddParameter("Speed", AnimatorControllerParameterType.Float);
            var machine = controller.layers[0].stateMachine;
            var states = new Dictionary<string, AnimatorState>(StringComparer.OrdinalIgnoreCase);
            int row = 0;
            foreach (var clip in clips)
            {
                var state = machine.AddState(clip.name, new Vector3(300, 60 * row++, 0));
                state.motion = clip;
                states[clip.name] = state;
            }
            AnimatorState idle, walk;
            states.TryGetValue(sidecar.idle ?? "Idle", out idle);
            states.TryGetValue(sidecar.walk ?? "Walk", out walk);
            if (idle != null)
                machine.defaultState = idle;
            else if (states.Count > 0)
                report.warnings.Add("no " + (sidecar.idle ?? "Idle") + " clip, so " + machine.defaultState.name + " plays first");
            if (idle != null && walk != null)
            {
                var go = idle.AddTransition(walk);
                go.AddCondition(AnimatorConditionMode.Greater, 0.1f, "Speed");
                go.hasExitTime = false;
                go.duration = 0.15f;
                var stop = walk.AddTransition(idle);
                stop.AddCondition(AnimatorConditionMode.Less, 0.1f, "Speed");
                stop.hasExitTime = false;
                stop.duration = 0.15f;
            }
            EditorUtility.SetDirty(controller);
            return controller;
        }

        // Four frames of a clip on the character, side by side, as a PNG.
        static string Picture(Scene stage, GameObject instance, Animator animator, AnimationClip clip, string name,
                              ClipReport entry)
        {
            const int cell = 320;
            var bounds = new Bounds(instance.transform.position, Vector3.zero);
            foreach (var r in instance.GetComponentsInChildren<Renderer>())
                bounds.Encapsulate(r.bounds);
            float height = Mathf.Max(Mathf.Max(bounds.size.x, bounds.size.y), Mathf.Max(bounds.size.z, 0.05f));

            var cameraObject = new GameObject("ARIA Preview Camera");
            SceneManager.MoveGameObjectToScene(cameraObject, stage);
            var camera = cameraObject.AddComponent<Camera>();
            camera.scene = stage;
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = new Color(0.36f, 0.38f, 0.42f);
            camera.orthographic = true;
            camera.orthographicSize = height * 0.62f;
            var lightObject = new GameObject("ARIA Preview Light");
            SceneManager.MoveGameObjectToScene(lightObject, stage);
            var light = lightObject.AddComponent<Light>();
            light.type = LightType.Directional;
            light.intensity = 1.3f;
            lightObject.transform.rotation = Quaternion.Euler(35, -150, 0);

            var target = new RenderTexture(cell, cell, 24);
            camera.targetTexture = target;
            var sheet = new Texture2D(cell * 4, cell, TextureFormat.RGB24, false);

            // The editor's own sampler. A PlayableGraph set to a time and
            // evaluated drew the same stride in every frame (measured).
            // The full-detail mesh in every picture, whatever size it comes out.
            // (ForceLOD was not enough: a lighter LOD, not re-skinned per sampled
            // frame, drew one pose four times -- measured, while LOD0's baked
            // mesh walked.) The prefab is saved already; this is the preview copy.
            foreach (var group in instance.GetComponentsInChildren<LODGroup>())
            {
                var levels = group.GetLODs();
                for (int level = 1; level < levels.Length; level++)
                    foreach (var r in levels[level].renderers)
                        if (r != null) r.enabled = false;
                group.enabled = false;
            }
            // Skinning is otherwise refreshed on the editor's schedule, not per
            // render: a game-ready character drew one pose in all four frames
            // while its baked mesh walked (measured).
            foreach (var skin in instance.GetComponentsInChildren<SkinnedMeshRenderer>(true))
                skin.forceMatrixRecalculationPerRender = true;
            var feet = new List<float>();
            var meshFeet = new List<float>();
            bool wasAnimating = AnimationMode.InAnimationMode() || clip == null;
            if (!wasAnimating)
                AnimationMode.StartAnimationMode();
            try
            {
                for (int i = 0; i < 4; i++)
                {
                    Vector3 from;
                    var t = instance.transform;
                    if (clip != null)
                    {
                        AnimationMode.BeginSampling();
                        AnimationMode.SampleAnimationClip(instance, clip, clip.length * i / 4f);
                        AnimationMode.EndSampling();
                        var foot = animator != null && animator.isHuman ? animator.GetBoneTransform(HumanBodyBones.LeftFoot) : null;
                        if (foot != null)
                            feet.Add(Mathf.Round(foot.position.z * 100f) / 100f);
                        var hipsBone = animator != null && animator.isHuman ? animator.GetBoneTransform(HumanBodyBones.Hips) : null;
                        if (entry != null && i == 0)
                            entry.pose = "root " + instance.transform.eulerAngles.ToString("F0") + " hipsUp "
                                + (hipsBone != null ? hipsBone.up.ToString("F2") : "?")
                                + " headAboveHips " + (hipsBone != null && animator.GetBoneTransform(HumanBodyBones.Head) != null
                                    ? (animator.GetBoneTransform(HumanBodyBones.Head).position - hipsBone.position).normalized.ToString("F2") : "?");
                        // The drawn mesh, not the bone: the lowest left-side vertex of the
                        // first skinned mesh, after skinning.
                        var skin = instance.GetComponentInChildren<SkinnedMeshRenderer>();
                        if (skin != null && entry != null)
                        {
                            var baked = new Mesh();
                            skin.BakeMesh(baked, true);
                            var low = baked.vertices.Select(v => skin.transform.TransformPoint(v))
                                .Where(v => v.x > instance.transform.position.x).OrderBy(v => v.y).FirstOrDefault();
                            meshFeet.Add(Mathf.Round(low.z * 100f) / 100f);
                            UnityEngine.Object.DestroyImmediate(baked);
                        }
                        // From the character's side for the first frames and its front
                        // for the last: a walk reads from the side, a wave from the front.
                        from = i < 3 ? (t.right + t.forward * 0.6f).normalized : t.forward;
                    }
                    else
                    {
                        // A prop: front, side, back, three-quarter from above.
                        from = i == 0 ? t.forward : i == 1 ? t.right : i == 2 ? -t.forward
                             : (t.forward + t.right + t.up * 0.8f).normalized;
                    }
                    var centre = bounds.center;
                    cameraObject.transform.position = centre + from * (height * 3f);
                    cameraObject.transform.LookAt(centre);
                    camera.Render();
                    RenderTexture.active = target;
                    sheet.ReadPixels(new Rect(0, 0, cell, cell), cell * i, 0);
                    RenderTexture.active = null;
                }
            }
            finally
            {
                if (!wasAnimating)
                    AnimationMode.StopAnimationMode();
                camera.targetTexture = null;
                UnityEngine.Object.DestroyImmediate(target);
            }
            sheet.Apply();
            string path = Path.Combine(ReportFolder(), name + "_" + (clip != null ? clip.name.Replace('|', '_').Replace(' ', '_') : "views") + ".png");
            Directory.CreateDirectory(ReportFolder());
            File.WriteAllBytes(path, sheet.EncodeToPNG());
            UnityEngine.Object.DestroyImmediate(sheet);
            UnityEngine.Object.DestroyImmediate(cameraObject);
            UnityEngine.Object.DestroyImmediate(lightObject);
            if (entry != null)
            {
                entry.leftFootZ = feet.ToArray();
                entry.meshFootZ = meshFeet.ToArray();
            }
            return path;
        }
    }
}
