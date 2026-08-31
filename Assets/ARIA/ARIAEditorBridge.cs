// Assets/ARIA/ARIAEditorBridge.cs
//
// ARIA's way into the Unity Editor.
//
// Every method here is static, takes only strings, and returns a JSON
// string. That shape is deliberate: it is the smallest surface that a
// process outside Unity can drive by reflection or by a command router,
// without either side needing the other's types.
//
// WHAT THIS IS NOT
// ----------------
// It is not a way for a model to run arbitrary code. Each method does
// one named thing with validated arguments -- the same rule ARIA's file
// tools follow, for the same reason. There is no Eval, no method
// dispatch by arbitrary name, and no path that reaches outside the
// project's Assets folder.
//
// EVERY METHOD ANSWERS, EVEN WHEN IT FAILS
// ----------------------------------------
// A caller across a process boundary cannot see an exception. So no
// method throws: each returns {"ok":false,"error":"..."} and logs, and
// the caller always has something to read. Silence is the one outcome
// that cannot be handled on the other side.

#if UNITY_EDITOR

using System;
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace ARIA
{
    /// <summary>
    /// Exposes Unity Editor operations to ARIA as string-in, JSON-out
    /// static methods. Loaded automatically when the editor starts.
    /// </summary>
    [InitializeOnLoad]
    public static class ARIAEditorBridge
    {
        /// <summary>The folder every asset path written here must sit inside.</summary>
        public const string AssetsRoot = "Assets";

        /// <summary>Announces itself once, so a user can see the bridge is present.</summary>
        static ARIAEditorBridge()
        {
            Debug.Log("[ARIA] Editor bridge loaded.");
        }

        #region Results

        /// <summary>One method's outcome, as the caller receives it.</summary>
        [Serializable]
        private class Result
        {
            public bool ok;
            public string error;
            public string value;
        }

        private static string Ok(string value = "")
        {
            return JsonUtility.ToJson(new Result { ok = true, error = "", value = value ?? "" });
        }

        /// <summary>
        /// A failure the caller can read, and a console line a human can.
        /// Never throws: an exception cannot cross the process boundary
        /// this bridge exists to serve.
        /// </summary>
        private static string Fail(string message)
        {
            Debug.LogError("[ARIA] " + message);
            return JsonUtility.ToJson(new Result { ok = false, error = message ?? "failed", value = "" });
        }

        #endregion

        #region Guards

        /// <summary>Whether a required string argument was actually supplied.</summary>
        private static bool Missing(string value)
        {
            return string.IsNullOrWhiteSpace(value);
        }

        /// <summary>
        /// Whether an asset path is inside Assets/ and free of traversal.
        /// The same confinement ARIA's file tools enforce; a bridge that
        /// wrote outside the project would be a hole in it.
        /// </summary>
        private static bool IsSafeAssetPath(string path)
        {
            if (Missing(path)) return false;

            string normalised = path.Replace('\\', '/').Trim();
            if (normalised.Contains("..")) return false;
            if (Path.IsPathRooted(normalised)) return false;

            return normalised == AssetsRoot || normalised.StartsWith(AssetsRoot + "/", StringComparison.Ordinal);
        }

        /// <summary>Finds a GameObject in the open scene by name, or null.</summary>
        private static GameObject FindInScene(string gameObjectName)
        {
            if (Missing(gameObjectName)) return null;

            // Includes inactive objects, which GameObject.Find does not --
            // an object the user just disabled is still one ARIA was asked
            // about.
            foreach (GameObject candidate in Resources.FindObjectsOfTypeAll<GameObject>())
            {
                if (candidate == null) continue;
                if (candidate.scene.IsValid() && candidate.name == gameObjectName)
                {
                    return candidate;
                }
            }
            return null;
        }

        /// <summary>
        /// Resolves a component or asset type by name, searching every
        /// loaded assembly. Returns null when nothing matches, which the
        /// callers report rather than assume.
        /// </summary>
        private static Type ResolveType(string typeName)
        {
            if (Missing(typeName)) return null;

            Type direct = Type.GetType(typeName, false, true);
            if (direct != null) return direct;

            foreach (System.Reflection.Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type found = assembly.GetType(typeName, false, true);
                if (found != null) return found;

                foreach (Type candidate in assembly.GetTypes())
                {
                    if (string.Equals(candidate.Name, typeName, StringComparison.OrdinalIgnoreCase))
                    {
                        return candidate;
                    }
                }
            }
            return null;
        }

        /// <summary>Makes sure the folders leading to an asset path exist.</summary>
        private static void EnsureFolder(string assetPath)
        {
            string folder = Path.GetDirectoryName(assetPath);
            if (Missing(folder)) return;

            folder = folder.Replace('\\', '/');
            if (AssetDatabase.IsValidFolder(folder)) return;

            string[] parts = folder.Split('/');
            string built = parts[0];
            for (int index = 1; index < parts.Length; index++)
            {
                string next = built + "/" + parts[index];
                if (!AssetDatabase.IsValidFolder(next))
                {
                    AssetDatabase.CreateFolder(built, parts[index]);
                }
                built = next;
            }
        }

        #endregion

        #region Scene objects

        /// <summary>
        /// Creates an empty GameObject in the open scene.
        /// </summary>
        /// <param name="name">The object's name. Required.</param>
        /// <returns>JSON: ok, error, and the created object's name.</returns>
        public static string CreateGameObject(string name)
        {
            if (Missing(name)) return Fail("CreateGameObject needs a name.");

            try
            {
                GameObject created = new GameObject(name);
                Undo.RegisterCreatedObjectUndo(created, "ARIA: create " + name);
                EditorSceneManager.MarkSceneDirty(created.scene);
                return Ok(created.name);
            }
            catch (Exception error)
            {
                return Fail("CreateGameObject failed: " + error.Message);
            }
        }

        /// <summary>
        /// Adds a component to a GameObject in the open scene.
        /// </summary>
        /// <param name="gameObjectName">The object to add to. Required.</param>
        /// <param name="componentType">The component's type name, e.g. "Rigidbody". Required.</param>
        /// <returns>JSON: ok, error, and the component's type name.</returns>
        public static string AddComponent(string gameObjectName, string componentType)
        {
            if (Missing(gameObjectName)) return Fail("AddComponent needs a gameObjectName.");
            if (Missing(componentType)) return Fail("AddComponent needs a componentType.");

            GameObject target = FindInScene(gameObjectName);
            if (target == null) return Fail("No GameObject named '" + gameObjectName + "' in the open scene.");

            Type resolved = ResolveType(componentType);
            if (resolved == null) return Fail("No type named '" + componentType + "' in any loaded assembly.");
            if (!typeof(Component).IsAssignableFrom(resolved))
            {
                return Fail("'" + componentType + "' is not a Component.");
            }

            if (target.GetComponent(resolved) != null)
            {
                Debug.LogWarning("[ARIA] '" + gameObjectName + "' already has a " + resolved.Name + "; not adding a second.");
                return Ok(resolved.Name);
            }

            try
            {
                Undo.AddComponent(target, resolved);
                EditorSceneManager.MarkSceneDirty(target.scene);
                return Ok(resolved.Name);
            }
            catch (Exception error)
            {
                return Fail("AddComponent failed: " + error.Message);
            }
        }

        /// <summary>
        /// Sets one serialized field on a component, by name.
        /// </summary>
        /// <param name="gameObjectName">The object holding the component. Required.</param>
        /// <param name="componentType">The component's type name. Required.</param>
        /// <param name="fieldName">The serialized field's name. Required.</param>
        /// <param name="value">The value, as text. Parsed to the field's type.</param>
        /// <returns>JSON: ok, error, and the value that was written.</returns>
        public static string SetSerializedField(string gameObjectName, string componentType,
                                                string fieldName, string value)
        {
            if (Missing(gameObjectName)) return Fail("SetSerializedField needs a gameObjectName.");
            if (Missing(componentType)) return Fail("SetSerializedField needs a componentType.");
            if (Missing(fieldName)) return Fail("SetSerializedField needs a fieldName.");

            GameObject target = FindInScene(gameObjectName);
            if (target == null) return Fail("No GameObject named '" + gameObjectName + "' in the open scene.");

            Type resolved = ResolveType(componentType);
            if (resolved == null) return Fail("No type named '" + componentType + "'.");

            Component component = target.GetComponent(resolved);
            if (component == null) return Fail("'" + gameObjectName + "' has no " + resolved.Name + ".");

            try
            {
                SerializedObject serialized = new SerializedObject(component);
                SerializedProperty property = serialized.FindProperty(fieldName);
                if (property == null)
                {
                    return Fail("'" + resolved.Name + "' has no serialized field '" + fieldName + "'.");
                }

                // Only the types a string can carry unambiguously. Anything
                // else is refused by name rather than guessed at.
                switch (property.propertyType)
                {
                    case SerializedPropertyType.Integer:
                        int parsedInt;
                        if (!int.TryParse(value, out parsedInt)) return Fail("'" + value + "' is not an integer.");
                        property.intValue = parsedInt;
                        break;

                    case SerializedPropertyType.Float:
                        float parsedFloat;
                        if (!float.TryParse(value, out parsedFloat)) return Fail("'" + value + "' is not a number.");
                        property.floatValue = parsedFloat;
                        break;

                    case SerializedPropertyType.Boolean:
                        bool parsedBool;
                        if (!bool.TryParse(value, out parsedBool)) return Fail("'" + value + "' is not true or false.");
                        property.boolValue = parsedBool;
                        break;

                    case SerializedPropertyType.String:
                        property.stringValue = value ?? "";
                        break;

                    default:
                        return Fail("Field '" + fieldName + "' is a " + property.propertyType +
                                    ", which this bridge does not set from text.");
                }

                serialized.ApplyModifiedProperties();
                EditorUtility.SetDirty(component);
                EditorSceneManager.MarkSceneDirty(target.scene);
                return Ok(value ?? "");
            }
            catch (Exception error)
            {
                return Fail("SetSerializedField failed: " + error.Message);
            }
        }

        #endregion

        #region Assets

        /// <summary>
        /// Saves a scene GameObject as a prefab asset.
        /// </summary>
        /// <param name="prefabPath">Where to write it, under Assets/, ending .prefab. Required.</param>
        /// <param name="gameObjectName">The scene object to save. Required.</param>
        /// <returns>JSON: ok, error, and the prefab's path.</returns>
        public static string CreatePrefab(string prefabPath, string gameObjectName)
        {
            if (!IsSafeAssetPath(prefabPath)) return Fail("prefabPath must be inside Assets/ and free of '..'.");
            if (Missing(gameObjectName)) return Fail("CreatePrefab needs a gameObjectName.");
            if (!prefabPath.EndsWith(".prefab", StringComparison.OrdinalIgnoreCase))
            {
                return Fail("prefabPath must end with .prefab.");
            }

            GameObject target = FindInScene(gameObjectName);
            if (target == null) return Fail("No GameObject named '" + gameObjectName + "' in the open scene.");

            try
            {
                EnsureFolder(prefabPath);
                bool saved;
                PrefabUtility.SaveAsPrefabAsset(target, prefabPath, out saved);
                if (!saved) return Fail("Unity refused to save the prefab at " + prefabPath + ".");

                AssetDatabase.SaveAssets();
                return Ok(prefabPath);
            }
            catch (Exception error)
            {
                return Fail("CreatePrefab failed: " + error.Message);
            }
        }

        /// <summary>
        /// Creates a ScriptableObject asset of a named type.
        /// </summary>
        /// <param name="typeName">The ScriptableObject subclass. Required.</param>
        /// <param name="assetPath">Where to write it, under Assets/, ending .asset. Required.</param>
        /// <returns>JSON: ok, error, and the asset's path.</returns>
        public static string CreateScriptableObject(string typeName, string assetPath)
        {
            if (Missing(typeName)) return Fail("CreateScriptableObject needs a typeName.");
            if (!IsSafeAssetPath(assetPath)) return Fail("assetPath must be inside Assets/ and free of '..'.");
            if (!assetPath.EndsWith(".asset", StringComparison.OrdinalIgnoreCase))
            {
                return Fail("assetPath must end with .asset.");
            }

            Type resolved = ResolveType(typeName);
            if (resolved == null) return Fail("No type named '" + typeName + "'.");
            if (!typeof(ScriptableObject).IsAssignableFrom(resolved))
            {
                return Fail("'" + typeName + "' is not a ScriptableObject.");
            }

            try
            {
                ScriptableObject asset = ScriptableObject.CreateInstance(resolved);
                if (asset == null) return Fail("Unity could not instantiate '" + typeName + "'.");

                EnsureFolder(assetPath);
                AssetDatabase.CreateAsset(asset, assetPath);
                AssetDatabase.SaveAssets();
                return Ok(assetPath);
            }
            catch (Exception error)
            {
                return Fail("CreateScriptableObject failed: " + error.Message);
            }
        }

        /// <summary>
        /// Imports or re-imports an asset already on disk.
        /// </summary>
        /// <param name="assetPath">The asset's path, under Assets/. Required.</param>
        /// <returns>JSON: ok, error, and the imported path.</returns>
        public static string ImportAsset(string assetPath)
        {
            if (!IsSafeAssetPath(assetPath)) return Fail("assetPath must be inside Assets/ and free of '..'.");

            if (!File.Exists(assetPath) && !AssetDatabase.IsValidFolder(assetPath))
            {
                return Fail("Nothing exists at " + assetPath + ".");
            }

            try
            {
                AssetDatabase.ImportAsset(assetPath, ImportAssetOptions.ForceUpdate);
                return Ok(assetPath);
            }
            catch (Exception error)
            {
                return Fail("ImportAsset failed: " + error.Message);
            }
        }

        #endregion

        #region Scenes

        /// <summary>
        /// Opens a scene, prompting to save the current one first.
        /// </summary>
        /// <param name="scenePath">The scene's path, under Assets/, ending .unity. Required.</param>
        /// <returns>JSON: ok, error, and the opened scene's path.</returns>
        public static string LoadScene(string scenePath)
        {
            if (!IsSafeAssetPath(scenePath)) return Fail("scenePath must be inside Assets/ and free of '..'.");
            if (!scenePath.EndsWith(".unity", StringComparison.OrdinalIgnoreCase))
            {
                return Fail("scenePath must end with .unity.");
            }
            if (!File.Exists(scenePath)) return Fail("No scene at " + scenePath + ".");

            try
            {
                // Asks about unsaved work rather than discarding it. A
                // bridge that silently threw away an hour of scene edits
                // would be worse than one that refused.
                if (!EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo())
                {
                    return Fail("The user cancelled saving the open scene; not loading " + scenePath + ".");
                }

                Scene opened = EditorSceneManager.OpenScene(scenePath, OpenSceneMode.Single);
                if (!opened.IsValid()) return Fail("Unity could not open " + scenePath + ".");
                return Ok(opened.path);
            }
            catch (Exception error)
            {
                return Fail("LoadScene failed: " + error.Message);
            }
        }

        /// <summary>
        /// Saves the currently open scene.
        /// </summary>
        /// <returns>JSON: ok, error, and the saved scene's path.</returns>
        public static string SaveScene()
        {
            try
            {
                Scene active = EditorSceneManager.GetActiveScene();
                if (!active.IsValid()) return Fail("There is no open scene to save.");

                if (Missing(active.path))
                {
                    return Fail("The open scene has never been saved; save it once in the editor first.");
                }

                if (!EditorSceneManager.SaveScene(active))
                {
                    return Fail("Unity refused to save " + active.path + ".");
                }
                return Ok(active.path);
            }
            catch (Exception error)
            {
                return Fail("SaveScene failed: " + error.Message);
            }
        }

        /// <summary>
        /// Describes the open scene's hierarchy as JSON.
        /// </summary>
        /// <returns>
        /// JSON: ok, error, and value holding {"scene","path","objects":[
        /// {"name","path","active","components":[...]}]}.
        /// </returns>
        public static string GetSceneSummary()
        {
            try
            {
                Scene active = EditorSceneManager.GetActiveScene();
                if (!active.IsValid()) return Fail("There is no open scene to describe.");

                SceneSummary summary = new SceneSummary
                {
                    scene = active.name,
                    path = active.path ?? "",
                    objects = new List<ObjectSummary>(),
                };

                foreach (GameObject root in active.GetRootGameObjects())
                {
                    Describe(root, "", summary.objects);
                }

                return Ok(JsonUtility.ToJson(summary));
            }
            catch (Exception error)
            {
                return Fail("GetSceneSummary failed: " + error.Message);
            }
        }

        /// <summary>Walks one object and its children into the summary.</summary>
        private static void Describe(GameObject target, string parentPath, List<ObjectSummary> into)
        {
            if (target == null) return;

            string path = string.IsNullOrEmpty(parentPath) ? target.name : parentPath + "/" + target.name;

            ObjectSummary described = new ObjectSummary
            {
                name = target.name,
                path = path,
                active = target.activeInHierarchy,
                components = new List<string>(),
            };

            foreach (Component component in target.GetComponents<Component>())
            {
                // A missing script is null here, and saying so is more use
                // than skipping it -- a broken reference is exactly what
                // somebody would be asking about.
                described.components.Add(component == null ? "<missing script>" : component.GetType().Name);
            }

            into.Add(described);

            foreach (Transform child in target.transform)
            {
                Describe(child.gameObject, path, into);
            }
        }

        /// <summary>One GameObject, as GetSceneSummary reports it.</summary>
        [Serializable]
        private class ObjectSummary
        {
            public string name;
            public string path;
            public bool active;
            public List<string> components;
        }

        /// <summary>One scene, as GetSceneSummary reports it.</summary>
        [Serializable]
        private class SceneSummary
        {
            public string scene;
            public string path;
            public List<ObjectSummary> objects;
        }

        #endregion

        #region Command line

        /// <summary>Opens the line ARIA reads its result from.</summary>
        public const string ResultOpen = "<<<ARIA_RESULT>>>";

        /// <summary>Closes it.</summary>
        public const string ResultClose = "<<<ARIA_END>>>";

        /// <summary>What ARIA passes in -ariaArgs.</summary>
        [Serializable]
        private class Request
        {
            public string method;
            public ArgumentBag args;
        }

        /// <summary>
        /// Every argument any bridge method takes, in one flat object.
        ///
        /// JsonUtility has no Dictionary support and no polymorphism, so
        /// a bag of named fields is the shape that actually crosses the
        /// boundary. Unset fields arrive as null and each method's own
        /// guards reject them, which is where that check belongs anyway.
        /// </summary>
        [Serializable]
        private class ArgumentBag
        {
            public string name;
            public string gameObjectName;
            public string componentType;
            public string fieldName;
            public string value;
            public string prefabPath;
            public string typeName;
            public string assetPath;
            public string scenePath;
            public string buildPath;
        }

        /// <summary>
        /// The one method ARIA executes. Reads -ariaArgs, dispatches, and
        /// prints the result between sentinels.
        /// </summary>
        /// <remarks>
        /// Unity's -executeMethod takes a parameterless static method and
        /// has no way to return a value, so the ten operations cannot be
        /// invoked directly from a command line. This is the door they
        /// are reached through.
        ///
        /// The sentinels are what make the answer findable. Batchmode
        /// stdout is licence checks, asset imports and shader warnings;
        /// something looking for "the JSON" would find the first brace in
        /// a log line.
        ///
        /// Exits 0 on success and 1 on failure, so a caller that cannot
        /// read stdout still learns something.
        /// </remarks>
        public static void RunFromCommandLine()
        {
            string result;
            try
            {
                result = Dispatch(ReadArgument());
            }
            catch (Exception error)
            {
                result = Fail("RunFromCommandLine failed: " + error.Message);
            }

            // Console.WriteLine rather than Debug.Log: Unity decorates a
            // log line with a stack trace, which would land inside the
            // sentinels and stop the result being JSON.
            Console.WriteLine(ResultOpen + result + ResultClose);
            Console.Out.Flush();

            if (Application.isBatchMode)
            {
                EditorApplication.Exit(result.Contains("\"ok\":true") ? 0 : 1);
            }
        }

        /// <summary>Pulls the -ariaArgs payload out of the command line.</summary>
        private static string ReadArgument()
        {
            foreach (string argument in Environment.GetCommandLineArgs())
            {
                if (argument != null && argument.StartsWith("-ariaArgs=", StringComparison.Ordinal))
                {
                    return argument.Substring("-ariaArgs=".Length);
                }
            }
            return "";
        }

        /// <summary>Routes one request to the method it names.</summary>
        private static string Dispatch(string payload)
        {
            if (Missing(payload)) return Fail("No -ariaArgs was supplied.");

            Request request;
            try
            {
                request = JsonUtility.FromJson<Request>(payload);
            }
            catch (Exception error)
            {
                return Fail("-ariaArgs was not valid JSON: " + error.Message);
            }

            if (request == null || Missing(request.method))
            {
                return Fail("-ariaArgs must name a method.");
            }

            ArgumentBag args = request.args ?? new ArgumentBag();

            // Named explicitly rather than dispatched by reflection. A
            // reflective call on a name from outside the editor is a way
            // to run any static method in the project, and this bridge
            // does exactly ten things on purpose.
            switch (request.method)
            {
                case "CreateGameObject":
                    return CreateGameObject(args.name);
                case "AddComponent":
                    return AddComponent(args.gameObjectName, args.componentType);
                case "CreatePrefab":
                    return CreatePrefab(args.prefabPath, args.gameObjectName);
                case "CreateScriptableObject":
                    return CreateScriptableObject(args.typeName, args.assetPath);
                case "LoadScene":
                    return LoadScene(args.scenePath);
                case "SaveScene":
                    return SaveScene();
                case "RunBuild":
                    return RunBuild(args.buildPath);
                case "ImportAsset":
                    return ImportAsset(args.assetPath);
                case "SetSerializedField":
                    return SetSerializedField(args.gameObjectName, args.componentType,
                                              args.fieldName, args.value);
                case "GetSceneSummary":
                    return GetSceneSummary();
                default:
                    return Fail("'" + request.method + "' is not a bridge method.");
            }
        }

        #endregion

        #region Build

        /// <summary>
        /// Builds the project's enabled scenes to a path.
        /// </summary>
        /// <param name="buildPath">Where to write the build. Required.</param>
        /// <returns>JSON: ok, error, and the build's result summary.</returns>
        public static string RunBuild(string buildPath)
        {
            if (Missing(buildPath)) return Fail("RunBuild needs a buildPath.");
            if (buildPath.Contains("..")) return Fail("buildPath must not contain '..'.");

            List<string> scenes = new List<string>();
            foreach (EditorBuildSettingsScene scene in EditorBuildSettings.scenes)
            {
                if (scene != null && scene.enabled && !Missing(scene.path))
                {
                    scenes.Add(scene.path);
                }
            }

            if (scenes.Count == 0)
            {
                return Fail("No enabled scenes in Build Settings; there is nothing to build.");
            }

            try
            {
                BuildPlayerOptions options = new BuildPlayerOptions
                {
                    scenes = scenes.ToArray(),
                    locationPathName = buildPath,
                    target = EditorUserBuildSettings.activeBuildTarget,
                    options = BuildOptions.None,
                };

                UnityEditor.Build.Reporting.BuildReport report = BuildPipeline.BuildPlayer(options);
                UnityEditor.Build.Reporting.BuildSummary built = report.summary;

                if (built.result != UnityEditor.Build.Reporting.BuildResult.Succeeded)
                {
                    return Fail("Build " + built.result + " with " + built.totalErrors + " error(s).");
                }

                return Ok(built.result + ", " + built.totalTime.TotalSeconds.ToString("F1") + "s");
            }
            catch (Exception error)
            {
                return Fail("RunBuild failed: " + error.Message);
            }
        }

        #endregion
    }
}

#endif
