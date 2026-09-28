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
        public List<string> warnings = new List<string>();
        public string error;
        public string builtAt;
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
            if (ReadSidecar(assetPath) == null)
                return;
            var importer = (ModelImporter)assetImporter;
            importer.animationType = ModelImporterAnimationType.Human;
            importer.avatarSetup = ModelImporterAvatarSetup.CreateFromThisModel;
            importer.importAnimation = true;
            importer.optimizeGameObjects = false;
        }

        void OnPreprocessAnimation()
        {
            var sidecar = ReadSidecar(assetPath);
            if (sidecar == null)
                return;
            var importer = (ModelImporter)assetImporter;
            var loops = new HashSet<string>((sidecar.loop ?? new string[0]).Select(l => l.ToLowerInvariant()));
            var clips = importer.defaultClipAnimations;
            foreach (var clip in clips)
            {
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
            report.humanoid = importer.animationType == ModelImporterAnimationType.Human;

            var assets = AssetDatabase.LoadAllAssetsAtPath(modelPath);
            var avatar = assets.OfType<Avatar>().FirstOrDefault();
            report.avatarValid = avatar != null && avatar.isValid;
            report.avatarHuman = avatar != null && avatar.isHuman;
            // HumanTrait names ("LeftUpperArm", or "Left Upper Arm" in places): compared without spaces.
            var description = avatar != null ? avatar.humanDescription : importer.humanDescription;
            var mapped = new HashSet<string>((description.human ?? new HumanBone[0]).Select(h => h.humanName.Replace(" ", "")));
            report.humanBones = mapped.Count;
            report.missingBones = RequiredBones.Where(b => !mapped.Contains(b)).ToArray();
            if (!report.avatarHuman)
                report.warnings.Add("the avatar is not a valid Humanoid -- clips from other characters will not play on it");

            var clips = assets.OfType<AnimationClip>().Where(c => !c.name.StartsWith("__preview__")).ToList();
            string folder = Path.GetDirectoryName(modelPath).Replace('\\', '/');
            string controllerPath = folder + "/" + sidecar.name + ".controller";
            var controller = BuildController(controllerPath, clips, sidecar, report);
            report.controller = controllerPath;

            string prefabPath = folder + "/" + sidecar.name + ".prefab";
            Scene stage = EditorSceneManager.NewPreviewScene();
            try
            {
                var instance = (GameObject)PrefabUtility.InstantiatePrefab(model, stage);
                instance.name = sidecar.name;
                var animator = instance.GetComponent<Animator>() ?? instance.AddComponent<Animator>();
                animator.runtimeAnimatorController = controller;
                animator.avatar = avatar;
                animator.applyRootMotion = false;
                PrefabUtility.SaveAsPrefabAsset(instance, prefabPath);
                report.prefab = prefabPath;
                foreach (var material in instance.GetComponentsInChildren<Renderer>()
                             .SelectMany(r => r.sharedMaterials).Where(m => m != null).Distinct())
                {
                    string shader = material.shader != null ? material.shader.name : "none";
                    report.materials.Add(material.name + ": " + shader);
                    if (shader.StartsWith("Hidden/InternalErrorShader") || !material.shader.isSupported)
                        report.warnings.Add(material.name + " has a shader this project cannot draw (" + shader
                                            + ") -- it shows magenta");
                }

                foreach (var clip in clips)
                {
                    var settings = AnimationUtility.GetAnimationClipSettings(clip);
                    report.clips.Add(new ClipReport { name = clip.name, loop = settings.loopTime, length = clip.length });
                }
                if (render)
                {
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
            float height = Mathf.Max(bounds.size.y, 0.5f);

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
            var feet = new List<float>();
            bool wasAnimating = AnimationMode.InAnimationMode();
            if (!wasAnimating)
                AnimationMode.StartAnimationMode();
            try
            {
                for (int i = 0; i < 4; i++)
                {
                    AnimationMode.BeginSampling();
                    AnimationMode.SampleAnimationClip(instance, clip, clip.length * i / 4f);
                    AnimationMode.EndSampling();
                    var foot = animator.isHuman ? animator.GetBoneTransform(HumanBodyBones.LeftFoot) : null;
                    if (foot != null)
                        feet.Add(Mathf.Round(foot.position.z * 100f) / 100f);
                    // From the character's side for the first frames and its front
                    // for the last: a walk reads from the side, a wave from the front.
                    var centre = bounds.center;
                    Vector3 from = i < 3 ? (instance.transform.right + instance.transform.forward * 0.6f).normalized : instance.transform.forward;
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
            string path = Path.Combine(ReportFolder(), name + "_" + clip.name.Replace('|', '_').Replace(' ', '_') + ".png");
            Directory.CreateDirectory(ReportFolder());
            File.WriteAllBytes(path, sheet.EncodeToPNG());
            UnityEngine.Object.DestroyImmediate(sheet);
            UnityEngine.Object.DestroyImmediate(cameraObject);
            UnityEngine.Object.DestroyImmediate(lightObject);
            entry.leftFootZ = feet.ToArray();
            return path;
        }
    }
}
