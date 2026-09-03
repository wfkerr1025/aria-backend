// Assets/ARIA/Editor/ARIAEditorBridge.cs
//
// ARIA's file-based RPC into a running Unity Editor.
//
//   ARIA (Python) writes   <ProjectRoot>/ARIA/unity_commands.json
//   this bridge executes it and writes
//                          <ProjectRoot>/ARIA/unity_results.json
//
// ProjectRoot is the folder that holds Assets/, so the two files sit
// beside the project rather than inside it, and Unity never imports them.
//
// HOW IT IS TRIGGERED
// -------------------
// Three doors, all leading to RunPendingCommands():
//   * the menu item  ARIA > Run Bridge Commands
//   * a watcher on EditorApplication.update that looks for the commands
//     file twice a second (ARIA > Watch For Commands, on by default)
//   * -executeMethod ARIA.Bridge.ARIAEditorBridge.RunPendingCommands
//     from a batch-mode command line, for a project no editor has open.
//
// WHAT IT DELIBERATELY IS NOT
// ---------------------------
// It is not a way to run arbitrary code. Every command is dispatched by
// an explicit switch, never by reflection over a name from outside, and
// every asset path is confined to Assets/. SetField reaches serialized
// fields and public members of components, which is the inspector's own
// surface and nothing more.
//
// EVERY COMMAND ANSWERS
// ---------------------
// A caller on the other side of a file cannot see an exception, so no
// command throws out. Each yields {success, message, data}, and the
// envelope around them says whether the batch as a whole succeeded.
//
// This is the sibling of Assets/ARIA/ARIAEditorBridge.cs, the batch-mode
// bridge driven by -ariaArgs. That one starts an editor per call; this
// one talks to the editor the user already has open. They share a class
// name but not a namespace, so both can live in one project.

#if UNITY_EDITOR

using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using Object = UnityEngine.Object;

namespace ARIA.Bridge
{
    /// <summary>
    /// Reads JSON commands from the project's ARIA folder, runs them
    /// against the open editor, and writes JSON results back.
    /// </summary>
    [InitializeOnLoad]
    public static class ARIAEditorBridge
    {
        public const string Version = "1.0.0";

        /// <summary>Folder beside Assets/ that holds the two RPC files.</summary>
        public const string FolderName = "ARIA";
        public const string CommandsFileName = "unity_commands.json";
        public const string ResultsFileName = "unity_results.json";

        private const string MenuRun = "ARIA/Run Bridge Commands";
        private const string MenuWatch = "ARIA/Watch For Commands";
        private const string MenuFolder = "ARIA/Open Bridge Folder";
        private const string WatchPrefKey = "ARIA.Bridge.WatchForCommands";
        private const double PollIntervalSeconds = 0.5;

        private static double _lastPoll;

        public static string ProjectRoot
        {
            get { return Directory.GetParent(Application.dataPath).FullName; }
        }

        public static string BridgeFolder
        {
            get { return Path.Combine(ProjectRoot, FolderName); }
        }

        public static string CommandsPath
        {
            get { return Path.Combine(BridgeFolder, CommandsFileName); }
        }

        public static string ResultsPath
        {
            get { return Path.Combine(BridgeFolder, ResultsFileName); }
        }

        /// <summary>Whether the editor polls for a commands file on its own.</summary>
        public static bool Watching
        {
            get { return EditorPrefs.GetBool(WatchPrefKey, true); }
            set { EditorPrefs.SetBool(WatchPrefKey, value); }
        }

        static ARIAEditorBridge()
        {
            // Batch mode is driven by -executeMethod alone. A watcher there
            // would race the explicit call and overwrite its results.
            if (Application.isBatchMode) return;

            try
            {
                Directory.CreateDirectory(BridgeFolder);
            }
            catch (Exception error)
            {
                Debug.LogWarning("[ARIA] Could not create " + BridgeFolder + ": " + error.Message);
            }

            EditorApplication.update += Poll;
            Debug.Log("[ARIA] Editor bridge " + Version + " loaded. Commands: " + CommandsPath);
        }

        #region Triggers

        private static void Poll()
        {
            if (!Watching) return;

            double now = EditorApplication.timeSinceStartup;
            if (now - _lastPoll < PollIntervalSeconds) return;
            _lastPoll = now;

            if (EditorApplication.isPlayingOrWillChangePlaymode) return;
            if (EditorApplication.isCompiling || EditorApplication.isUpdating) return;
            if (!File.Exists(CommandsPath)) return;

            RunPendingCommands();
        }

        [MenuItem(MenuWatch)]
        private static void ToggleWatch()
        {
            Watching = !Watching;
            Debug.Log("[ARIA] Watch for commands: " + (Watching ? "on" : "off"));
        }

        [MenuItem(MenuWatch, true)]
        private static bool ToggleWatchValidate()
        {
            Menu.SetChecked(MenuWatch, Watching);
            return true;
        }

        [MenuItem(MenuFolder)]
        private static void OpenFolder()
        {
            Directory.CreateDirectory(BridgeFolder);
            EditorUtility.RevealInFinder(BridgeFolder);
        }

        /// <summary>
        /// Reads the commands file, runs it, writes the results file. Safe
        /// to call with no commands file present: that is reported too.
        /// </summary>
        [MenuItem(MenuRun)]
        public static void RunPendingCommands()
        {
            string requestId = null;
            List<CommandResult> results = new List<CommandResult>();
            bool ok;
            string message;

            if (EditorApplication.isPlayingOrWillChangePlaymode)
            {
                ok = false;
                message = "Refusing to run bridge commands in play mode. Stop the game first.";
            }
            else if (!File.Exists(CommandsPath))
            {
                ok = false;
                message = "No commands file at " + CommandsPath + ".";
            }
            else
            {
                string text;
                try
                {
                    text = File.ReadAllText(CommandsPath);
                }
                catch (IOException error)
                {
                    // Still being written. The watcher will try again;
                    // a batch-mode caller gets told.
                    Debug.LogWarning("[ARIA] Commands file is busy: " + error.Message);
                    if (Application.isBatchMode) EditorApplication.Exit(1);
                    return;
                }

                try
                {
                    File.Delete(CommandsPath);
                }
                catch (IOException error)
                {
                    Debug.LogWarning("[ARIA] Could not remove the commands file: " + error.Message);
                }

                ok = RunCommandText(text, results, out requestId, out message);
            }

            WriteResults(requestId, ok, message, results);
            Debug.Log("[ARIA] " + message + " Results: " + ResultsPath);

            if (Application.isBatchMode) EditorApplication.Exit(ok ? 0 : 1);
        }

        /// <summary>
        /// Runs a commands document given as text. Exposed so a test or a
        /// future transport can bypass the file without bypassing the rules.
        /// </summary>
        public static bool RunCommandText(string text, List<CommandResult> results,
                                          out string requestId, out string message)
        {
            requestId = null;

            object parsed;
            string parseError;
            if (!Json.TryParse(text, out parsed, out parseError))
            {
                message = "Commands file is not valid JSON: " + parseError;
                return false;
            }

            Dictionary<string, object> envelope = parsed as Dictionary<string, object>;
            List<object> commands = parsed as List<object>;
            bool stopOnError = true;

            if (envelope != null)
            {
                requestId = Str(envelope, "id");
                stopOnError = Bool(envelope, "stopOnError", true);

                object list;
                if (envelope.TryGetValue("commands", out list) && list is List<object>)
                {
                    commands = (List<object>)list;
                }
                else if (envelope.ContainsKey("command"))
                {
                    commands = new List<object> { envelope };
                }
            }

            if (commands == null)
            {
                message = "Commands must be {\"commands\":[...]} or {\"command\":\"...\",\"args\":{...}}.";
                return false;
            }

            Execute(commands, stopOnError, null, results);

            int failed = 0;
            foreach (CommandResult result in results)
            {
                if (!result.Success) failed++;
            }

            message = "Ran " + results.Count + " command(s): " + (results.Count - failed) +
                      " succeeded, " + failed + " failed.";
            return failed == 0;
        }

        private static void WriteResults(string requestId, bool ok, string message,
                                         List<CommandResult> results)
        {
            Dictionary<string, object> envelope = new Dictionary<string, object>();
            envelope["id"] = requestId;
            envelope["ok"] = ok;
            envelope["message"] = message;
            envelope["bridgeVersion"] = Version;
            envelope["unityVersion"] = Application.unityVersion;
            envelope["project"] = ProjectRoot;
            envelope["finishedAt"] = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);

            List<object> items = new List<object>();
            foreach (CommandResult result in results) items.Add(result.ToJson());
            envelope["results"] = items;

            string text = Json.Serialize(envelope, true);

            try
            {
                Directory.CreateDirectory(BridgeFolder);

                // Written whole and then swapped in, so a reader polling
                // for the file never sees half of it.
                string temp = ResultsPath + ".tmp";
                File.WriteAllText(temp, text, new UTF8Encoding(false));
                if (File.Exists(ResultsPath))
                {
                    File.Replace(temp, ResultsPath, null);
                }
                else
                {
                    File.Move(temp, ResultsPath);
                }
            }
            catch (Exception error)
            {
                Debug.LogError("[ARIA] Could not write results to " + ResultsPath + ": " + error.Message);
                try { File.WriteAllText(ResultsPath, text, new UTF8Encoding(false)); }
                catch (Exception) { /* nothing left to try */ }
            }
        }

        #endregion

        #region Dispatch

        /// <summary>What one command produced.</summary>
        public sealed class CommandResult
        {
            public string Command;
            public bool Success;
            public string Message;
            public Dictionary<string, object> Data;

            public Dictionary<string, object> ToJson()
            {
                Dictionary<string, object> json = new Dictionary<string, object>();
                json["command"] = Command ?? "";
                json["success"] = Success;
                json["message"] = Message ?? "";
                json["data"] = Data;
                return json;
            }
        }

        /// <summary>
        /// Where targets resolve. Null means the open scenes; a prefab root
        /// means the contents of a prefab being edited by ModifyPrefab.
        /// </summary>
        private sealed class Scope
        {
            public GameObject PrefabRoot;

            public bool InPrefab
            {
                get { return PrefabRoot != null; }
            }

            /// <summary>Undo only makes sense for scene objects.</summary>
            public bool Undo
            {
                get { return PrefabRoot == null; }
            }
        }

        private static void Execute(List<object> commands, bool stopOnError, Scope scope,
                                    List<CommandResult> into)
        {
            bool failed = false;

            foreach (object item in commands)
            {
                Dictionary<string, object> entry = item as Dictionary<string, object>;
                string name = entry != null ? Str(entry, "command", "cmd") : null;

                CommandResult result;
                if (entry == null)
                {
                    result = Fail("Each command must be an object with \"command\" and \"args\".");
                }
                else if (string.IsNullOrEmpty(name))
                {
                    result = Fail("Command entry has no \"command\" name.");
                }
                else if (failed && stopOnError)
                {
                    result = new CommandResult
                    {
                        Success = false,
                        Message = "Skipped: an earlier command failed and stopOnError is on.",
                    };
                }
                else
                {
                    Dictionary<string, object> args = null;
                    object rawArgs;
                    if (entry.TryGetValue("args", out rawArgs)) args = rawArgs as Dictionary<string, object>;
                    if (args == null) args = new Dictionary<string, object>();

                    try
                    {
                        result = RunOne(name, args, scope ?? new Scope());
                    }
                    catch (Exception error)
                    {
                        result = Fail(name + " threw " + error.GetType().Name + ": " + error.Message);
                    }
                }

                result.Command = name ?? "";
                if (!result.Success) failed = true;
                into.Add(result);
            }
        }

        /// <summary>
        /// The one place a command name becomes code. Explicit on purpose:
        /// nothing outside the editor can reach a method this list omits.
        /// </summary>
        private static CommandResult RunOne(string command, Dictionary<string, object> args, Scope scope)
        {
            switch (command)
            {
                case "Ping": return Ping();
                case "CreateGameObject": return CreateGameObject(args, scope);
                case "DeleteGameObject": return DeleteGameObject(args, scope);
                case "AddComponent": return AddComponent(args, scope);
                case "RemoveComponent": return RemoveComponent(args, scope);
                case "SetTransform": return SetTransform(args, scope);
                case "SetField": return SetField(args, scope);
                case "GetField": return GetField(args, scope);
                case "GetHierarchy": return GetHierarchy(args, scope);
                case "OpenScene": return OpenScene(args, scope);
                case "SaveScene": return SaveScene(args, scope);
                case "CreatePrefab": return CreatePrefab(args, scope);
                case "ModifyPrefab": return ModifyPrefab(args, scope);
                case "InstantiatePrefab": return InstantiatePrefab(args, scope);
                case "CreateLight": return CreateLight(args, scope);
                case "CreateCamera": return CreateCamera(args, scope);
                default:
                    return Fail("'" + command + "' is not a bridge command.");
            }
        }

        private static CommandResult Ok(string message, Dictionary<string, object> data)
        {
            return new CommandResult { Success = true, Message = message ?? "", Data = data };
        }

        private static CommandResult Fail(string message)
        {
            Debug.LogWarning("[ARIA] " + message);
            return new CommandResult { Success = false, Message = message ?? "failed", Data = null };
        }

        #endregion

        #region Commands: scene objects

        private static CommandResult Ping()
        {
            Dictionary<string, object> data = new Dictionary<string, object>();
            data["bridgeVersion"] = Version;
            data["unityVersion"] = Application.unityVersion;
            data["project"] = ProjectRoot;
            data["batchMode"] = Application.isBatchMode;
            data["isPlaying"] = EditorApplication.isPlaying;
            Scene active = SceneManager.GetActiveScene();
            data["activeScene"] = active.path ?? "";
            data["activeSceneName"] = active.name ?? "";
            return Ok("Bridge is up.", data);
        }

        private static CommandResult CreateGameObject(Dictionary<string, object> args, Scope scope)
        {
            string name = Str(args, "name");
            if (string.IsNullOrEmpty(name)) name = "GameObject";

            GameObject parent = null;
            string parentRef = Str(args, "parent", "parentPath");
            if (!string.IsNullOrEmpty(parentRef))
            {
                string error;
                parent = Resolve(parentRef, scope, out error);
                if (parent == null) return Fail("Parent not found: " + error);
            }
            else if (scope.InPrefab)
            {
                parent = scope.PrefabRoot;
            }

            GameObject created;
            string primitive = Str(args, "primitive");
            if (!string.IsNullOrEmpty(primitive))
            {
                PrimitiveType kind;
                if (!TryEnum(primitive, out kind))
                {
                    return Fail("'" + primitive + "' is not a primitive (Cube, Sphere, Capsule, Cylinder, Plane, Quad).");
                }
                created = GameObject.CreatePrimitive(kind);
                created.name = name;
            }
            else
            {
                created = new GameObject(name);
            }

            if (scope.Undo) Undo.RegisterCreatedObjectUndo(created, "ARIA: create " + name);
            if (parent != null) created.transform.SetParent(parent.transform, false);

            string transformError;
            if (!ApplyTransform(created.transform, args, true, out transformError))
            {
                Destroy(created, scope);
                return Fail(transformError);
            }

            object componentList;
            if (args.TryGetValue("components", out componentList) && componentList is List<object>)
            {
                foreach (object entry in (List<object>)componentList)
                {
                    string typeName = entry as string;
                    string addError;
                    if (AddComponentTo(created, typeName, scope, out addError) == null)
                    {
                        Destroy(created, scope);
                        return Fail(addError);
                    }
                }
            }

            MarkDirty(created);
            return Ok("Created " + HierarchyPath(created.transform) + ".", Describe(created));
        }

        private static CommandResult DeleteGameObject(Dictionary<string, object> args, Scope scope)
        {
            string error;
            GameObject target = ResolveTarget(args, scope, out error);
            if (target == null) return Fail(error);
            if (scope.InPrefab && target == scope.PrefabRoot) return Fail("Cannot delete a prefab's root object.");

            string path = HierarchyPath(target.transform);
            Scene scene = target.scene;
            Destroy(target, scope);
            if (scene.IsValid() && !EditorSceneManager.IsPreviewScene(scene)) EditorSceneManager.MarkSceneDirty(scene);

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["path"] = path;
            return Ok("Deleted " + path + ".", data);
        }

        private static CommandResult AddComponent(Dictionary<string, object> args, Scope scope)
        {
            string error;
            GameObject target = ResolveTarget(args, scope, out error);
            if (target == null) return Fail(error);

            string typeName = Str(args, "componentType", "component", "type");
            if (string.IsNullOrEmpty(typeName)) return Fail("AddComponent needs a componentType.");

            Type type = ResolveComponentType(typeName, out error);
            if (type == null) return Fail(error);

            Component existing = target.GetComponent(type);
            if (existing != null && !Bool(args, "allowDuplicate", false))
            {
                return Ok(target.name + " already has a " + type.Name + "; left as is.",
                          DescribeComponent(existing));
            }

            Component added = AddComponentTo(target, typeName, scope, out error);
            if (added == null) return Fail(error);

            MarkDirty(target);
            return Ok("Added " + type.Name + " to " + HierarchyPath(target.transform) + ".",
                      DescribeComponent(added));
        }

        private static CommandResult RemoveComponent(Dictionary<string, object> args, Scope scope)
        {
            string error;
            GameObject target = ResolveTarget(args, scope, out error);
            if (target == null) return Fail(error);

            string typeName = Str(args, "componentType", "component", "type");
            if (string.IsNullOrEmpty(typeName)) return Fail("RemoveComponent needs a componentType.");

            Type type = ResolveComponentType(typeName, out error);
            if (type == null) return Fail(error);
            if (type == typeof(Transform)) return Fail("A Transform cannot be removed.");

            Component component = target.GetComponent(type);
            if (component == null) return Fail(target.name + " has no " + type.Name + ".");

            if (scope.Undo) Undo.DestroyObjectImmediate(component);
            else Object.DestroyImmediate(component);

            if (target.GetComponent(type) != null)
            {
                return Fail("Unity refused to remove " + type.Name + " from " + target.name +
                            "; another component probably requires it.");
            }

            MarkDirty(target);
            Dictionary<string, object> data = new Dictionary<string, object>();
            data["path"] = HierarchyPath(target.transform);
            data["component"] = type.Name;
            return Ok("Removed " + type.Name + " from " + target.name + ".", data);
        }

        private static CommandResult SetTransform(Dictionary<string, object> args, Scope scope)
        {
            string error;
            GameObject target = ResolveTarget(args, scope, out error);
            if (target == null) return Fail(error);

            bool local = Bool(args, "local", false);
            if (scope.Undo) Undo.RecordObject(target.transform, "ARIA: set transform");

            if (!ApplyTransform(target.transform, args, local, out error)) return Fail(error);

            MarkDirty(target);
            return Ok("Transform set on " + HierarchyPath(target.transform) + ".",
                      DescribeTransform(target.transform));
        }

        private static CommandResult SetField(Dictionary<string, object> args, Scope scope)
        {
            string error;
            Object owner = ResolveMemberOwner(args, scope, out error);
            if (owner == null) return Fail(error);

            string field = Str(args, "field", "fieldName", "property", "name");
            if (string.IsNullOrEmpty(field)) return Fail("SetField needs a field name.");

            object value;
            if (!args.TryGetValue("value", out value))
            {
                return Fail("SetField needs a value (use null explicitly to clear a reference).");
            }

            GameObject asGameObject = owner as GameObject;
            if (asGameObject != null && IsActiveAlias(field))
            {
                bool active;
                if (!TryBool(value, out active)) return Fail("'active' needs true or false.");
                if (scope.Undo) Undo.RecordObject(asGameObject, "ARIA: set active");
                asGameObject.SetActive(active);
                MarkDirty(asGameObject);
                Dictionary<string, object> activeData = new Dictionary<string, object>();
                activeData["field"] = "active";
                activeData["value"] = active;
                return Ok((active ? "Activated " : "Deactivated ") + asGameObject.name + ".", activeData);
            }

            string written;
            if (!SetMember(owner, field, value, scope, out written, out error)) return Fail(error);

            object readBack;
            string readError;
            GetMember(owner, field, out readBack, out readError);

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["target"] = HierarchyPath(OwnerTransform(owner));
            data["type"] = owner.GetType().Name;
            data["field"] = written;
            data["value"] = readBack;
            return Ok("Set " + owner.GetType().Name + "." + written + ".", data);
        }

        private static CommandResult GetField(Dictionary<string, object> args, Scope scope)
        {
            string error;
            Object owner = ResolveMemberOwner(args, scope, out error);
            if (owner == null) return Fail(error);

            string field = Str(args, "field", "fieldName", "property", "name");
            if (string.IsNullOrEmpty(field)) return Fail("GetField needs a field name.");

            object value;
            if (!GetMember(owner, field, out value, out error)) return Fail(error);

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["target"] = HierarchyPath(OwnerTransform(owner));
            data["type"] = owner.GetType().Name;
            data["field"] = field;
            data["value"] = value;
            return Ok("Read " + owner.GetType().Name + "." + field + ".", data);
        }

        private static CommandResult GetHierarchy(Dictionary<string, object> args, Scope scope)
        {
            int depth = (int)Num(args, -1, "depth", "maxDepth");
            List<object> objects = new List<object>();
            Dictionary<string, object> data = new Dictionary<string, object>();

            string targetRef = Str(args, "target", "gameObject", "ref", "root");
            if (!string.IsNullOrEmpty(targetRef) || scope.InPrefab)
            {
                string error;
                GameObject root = Resolve(targetRef, scope, out error);
                if (root == null) return Fail(error);
                Describe(root, 0, depth, objects);
                data["root"] = HierarchyPath(root.transform);
            }
            else
            {
                List<object> scenes = new List<object>();
                for (int index = 0; index < SceneManager.sceneCount; index++)
                {
                    Scene scene = SceneManager.GetSceneAt(index);
                    if (!scene.isLoaded) continue;
                    Dictionary<string, object> described = new Dictionary<string, object>();
                    described["name"] = scene.name;
                    described["path"] = scene.path ?? "";
                    described["dirty"] = scene.isDirty;
                    scenes.Add(described);
                    foreach (GameObject root in scene.GetRootGameObjects()) Describe(root, 0, depth, objects);
                }
                data["scenes"] = scenes;
                data["activeScene"] = SceneManager.GetActiveScene().path ?? "";
            }

            data["objects"] = objects;
            return Ok(objects.Count + " object(s).", data);
        }

        #endregion

        #region Commands: scenes and prefabs

        private static CommandResult OpenScene(Dictionary<string, object> args, Scope scope)
        {
            if (scope.InPrefab) return Fail("OpenScene is not allowed inside ModifyPrefab.");

            string path = Str(args, "path", "scenePath", "scene");
            string error;
            if (!SafeAssetPath(path, ".unity", out error)) return Fail(error);
            if (!File.Exists(Path.Combine(ProjectRoot, path))) return Fail("No scene at " + path + ".");

            OpenSceneMode mode = OpenSceneMode.Single;
            string modeText = Str(args, "mode");
            if (Bool(args, "additive", false) ||
                string.Equals(modeText, "additive", StringComparison.OrdinalIgnoreCase))
            {
                mode = OpenSceneMode.Additive;
            }

            // Asks about unsaved work rather than discarding it, unless
            // the caller said discard or nobody is there to be asked.
            if (mode == OpenSceneMode.Single && !Application.isBatchMode &&
                !Bool(args, "discardChanges", false))
            {
                if (!EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo())
                {
                    return Fail("Cancelled: the open scene has unsaved changes and the user declined. " +
                                "Pass discardChanges:true to skip the prompt.");
                }
            }

            Scene opened = EditorSceneManager.OpenScene(path, mode);
            if (!opened.IsValid()) return Fail("Unity could not open " + path + ".");

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["path"] = opened.path;
            data["name"] = opened.name;
            data["rootCount"] = opened.rootCount;
            data["mode"] = mode.ToString();
            return Ok("Opened " + opened.path + ".", data);
        }

        private static CommandResult SaveScene(Dictionary<string, object> args, Scope scope)
        {
            if (scope.InPrefab) return Fail("SaveScene is not allowed inside ModifyPrefab.");

            if (Bool(args, "saveAll", false))
            {
                if (!EditorSceneManager.SaveOpenScenes()) return Fail("Unity refused to save the open scenes.");
                return Ok("Saved all open scenes.", null);
            }

            Scene active = SceneManager.GetActiveScene();
            if (!active.IsValid()) return Fail("There is no open scene to save.");

            string path = Str(args, "path", "scenePath", "scene");
            bool saved;
            if (!string.IsNullOrEmpty(path))
            {
                string error;
                if (!SafeAssetPath(path, ".unity", out error)) return Fail(error);
                EnsureFolder(path);
                saved = EditorSceneManager.SaveScene(active, path);
            }
            else
            {
                if (string.IsNullOrEmpty(active.path))
                {
                    return Fail("The open scene has never been saved; give SaveScene a path.");
                }
                saved = EditorSceneManager.SaveScene(active);
            }

            if (!saved) return Fail("Unity refused to save " + (path ?? active.path) + ".");

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["path"] = SceneManager.GetActiveScene().path;
            return Ok("Saved " + data["path"] + ".", data);
        }

        private static CommandResult CreatePrefab(Dictionary<string, object> args, Scope scope)
        {
            if (scope.InPrefab) return Fail("CreatePrefab is not allowed inside ModifyPrefab.");

            string error;
            GameObject target = ResolveTarget(args, scope, out error);
            if (target == null) return Fail(error);

            string prefabPath = Str(args, "prefabPath", "path", "assetPath");
            if (!SafeAssetPath(prefabPath, ".prefab", out error)) return Fail(error);

            EnsureFolder(prefabPath);
            bool saved;
            GameObject asset;
            if (Bool(args, "connect", true))
            {
                asset = PrefabUtility.SaveAsPrefabAssetAndConnect(target, prefabPath,
                                                                  InteractionMode.AutomatedAction, out saved);
            }
            else
            {
                asset = PrefabUtility.SaveAsPrefabAsset(target, prefabPath, out saved);
            }

            if (!saved || asset == null) return Fail("Unity refused to save a prefab at " + prefabPath + ".");
            AssetDatabase.SaveAssets();

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["prefabPath"] = prefabPath;
            data["guid"] = AssetDatabase.AssetPathToGUID(prefabPath);
            data["source"] = HierarchyPath(target.transform);
            return Ok("Saved prefab " + prefabPath + ".", data);
        }

        private static CommandResult ModifyPrefab(Dictionary<string, object> args, Scope scope)
        {
            if (scope.InPrefab) return Fail("ModifyPrefab cannot nest.");

            string prefabPath = Str(args, "prefabPath", "path", "assetPath");
            string error;
            if (!SafeAssetPath(prefabPath, ".prefab", out error)) return Fail(error);
            if (!File.Exists(Path.Combine(ProjectRoot, prefabPath))) return Fail("No prefab at " + prefabPath + ".");

            object rawOperations;
            List<object> operations = null;
            if (args.TryGetValue("operations", out rawOperations) || args.TryGetValue("commands", out rawOperations))
            {
                operations = rawOperations as List<object>;
            }
            if (operations == null) return Fail("ModifyPrefab needs an operations list of {command, args}.");

            GameObject root = PrefabUtility.LoadPrefabContents(prefabPath);
            if (root == null) return Fail("Unity could not load the contents of " + prefabPath + ".");

            List<CommandResult> inner = new List<CommandResult>();
            try
            {
                Execute(operations, Bool(args, "stopOnError", true), new Scope { PrefabRoot = root }, inner);

                int failed = 0;
                foreach (CommandResult result in inner)
                {
                    if (!result.Success) failed++;
                }

                List<object> reported = new List<object>();
                foreach (CommandResult result in inner) reported.Add(result.ToJson());

                Dictionary<string, object> data = new Dictionary<string, object>();
                data["prefabPath"] = prefabPath;
                data["results"] = reported;

                if (failed > 0)
                {
                    // Nothing is written: a prefab half-modified is worse
                    // than one untouched, and the caller can read why.
                    return new CommandResult
                    {
                        Success = false,
                        Message = failed + " of " + inner.Count + " operation(s) failed; " + prefabPath + " was not changed.",
                        Data = data,
                    };
                }

                bool saved;
                PrefabUtility.SaveAsPrefabAsset(root, prefabPath, out saved);
                if (!saved) return Fail("Unity refused to save " + prefabPath + " after modifying it.");
                AssetDatabase.SaveAssets();

                return Ok("Applied " + inner.Count + " operation(s) to " + prefabPath + ".", data);
            }
            finally
            {
                PrefabUtility.UnloadPrefabContents(root);
            }
        }

        private static CommandResult InstantiatePrefab(Dictionary<string, object> args, Scope scope)
        {
            if (scope.InPrefab) return Fail("InstantiatePrefab is not allowed inside ModifyPrefab.");

            string prefabPath = Str(args, "prefabPath", "path", "assetPath");
            string error;
            if (!SafeAssetPath(prefabPath, ".prefab", out error)) return Fail(error);

            GameObject asset = AssetDatabase.LoadAssetAtPath<GameObject>(prefabPath);
            if (asset == null) return Fail("No prefab at " + prefabPath + ".");

            GameObject parent = null;
            string parentRef = Str(args, "parent", "parentPath");
            if (!string.IsNullOrEmpty(parentRef))
            {
                parent = Resolve(parentRef, scope, out error);
                if (parent == null) return Fail("Parent not found: " + error);
            }

            GameObject instance = PrefabUtility.InstantiatePrefab(asset, SceneManager.GetActiveScene()) as GameObject;
            if (instance == null) return Fail("Unity could not instantiate " + prefabPath + ".");

            Undo.RegisterCreatedObjectUndo(instance, "ARIA: instantiate " + asset.name);
            if (parent != null) instance.transform.SetParent(parent.transform, false);

            string name = Str(args, "name");
            if (!string.IsNullOrEmpty(name)) instance.name = name;

            if (!ApplyTransform(instance.transform, args, true, out error))
            {
                Destroy(instance, scope);
                return Fail(error);
            }

            MarkDirty(instance);
            Dictionary<string, object> data = Describe(instance);
            data["prefabPath"] = prefabPath;
            return Ok("Instantiated " + prefabPath + " as " + HierarchyPath(instance.transform) + ".", data);
        }

        #endregion

        #region Commands: lights and cameras

        private static CommandResult CreateLight(Dictionary<string, object> args, Scope scope)
        {
            string typeText = Str(args, "type", "lightType");
            if (string.IsNullOrEmpty(typeText)) typeText = "Point";

            LightType lightType;
            if (!TryEnum(typeText, out lightType))
            {
                return Fail("'" + typeText + "' is not a light type (Directional, Point, Spot).");
            }

            Dictionary<string, object> creation = new Dictionary<string, object>(args);
            if (!creation.ContainsKey("name")) creation["name"] = lightType + " Light";
            creation.Remove("components");
            creation.Remove("primitive");

            // A directional light with no rotation points straight down
            // the Z axis, which lights nothing anyone can see. Unity's own
            // default is a tilt, so this uses the same one.
            if (lightType == LightType.Directional && !creation.ContainsKey("rotation"))
            {
                creation["rotation"] = new List<object> { 50.0, -30.0, 0.0 };
            }

            CommandResult created = CreateGameObject(creation, scope);
            if (!created.Success) return created;

            string error;
            GameObject holder = Resolve("id:" + created.Data["instanceId"], null, out error);
            if (holder == null) holder = LastCreated;
            if (holder == null) return Fail("Lost the light's object after creating it.");

            Light light = AddComponentTo(holder, "UnityEngine.Light", scope, out error) as Light;
            if (light == null) return Fail(error);

            light.type = lightType;
            light.intensity = (float)Num(args, 1.0, "intensity");

            Color color;
            object rawColor;
            if (args.TryGetValue("color", out rawColor) && rawColor != null)
            {
                if (!TryColor(rawColor, out color)) return Fail("'color' must be [r,g,b], [r,g,b,a], or a colour name/hex.");
                light.color = color;
            }

            if (args.ContainsKey("range")) light.range = (float)Num(args, light.range, "range");
            if (args.ContainsKey("spotAngle")) light.spotAngle = (float)Num(args, light.spotAngle, "spotAngle");

            string shadows = Str(args, "shadows");
            if (!string.IsNullOrEmpty(shadows))
            {
                LightShadows mode;
                if (!TryEnum(shadows, out mode)) return Fail("'shadows' must be None, Hard or Soft.");
                light.shadows = mode;
            }

            MarkDirty(holder);
            Dictionary<string, object> data = Describe(holder);
            data["lightType"] = light.type.ToString();
            data["intensity"] = light.intensity;
            data["color"] = light.color;
            return Ok("Created " + light.type + " light " + HierarchyPath(holder.transform) + ".", data);
        }

        private static CommandResult CreateCamera(Dictionary<string, object> args, Scope scope)
        {
            Dictionary<string, object> creation = new Dictionary<string, object>(args);
            if (!creation.ContainsKey("name")) creation["name"] = "Camera";
            creation.Remove("components");
            creation.Remove("primitive");

            CommandResult created = CreateGameObject(creation, scope);
            if (!created.Success) return created;

            string error;
            GameObject holder = Resolve("id:" + created.Data["instanceId"], null, out error);
            if (holder == null) holder = LastCreated;
            if (holder == null) return Fail("Lost the camera's object after creating it.");

            Camera camera = AddComponentTo(holder, "UnityEngine.Camera", scope, out error) as Camera;
            if (camera == null) return Fail(error);

            camera.fieldOfView = (float)Num(args, 60.0, "fov", "fieldOfView");
            if (args.ContainsKey("near") || args.ContainsKey("nearClip"))
            {
                camera.nearClipPlane = (float)Num(args, camera.nearClipPlane, "near", "nearClip");
            }
            if (args.ContainsKey("far") || args.ContainsKey("farClip"))
            {
                camera.farClipPlane = (float)Num(args, camera.farClipPlane, "far", "farClip");
            }

            string clear = Str(args, "clearFlags");
            if (!string.IsNullOrEmpty(clear))
            {
                CameraClearFlags flags;
                if (!TryEnum(clear, out flags)) return Fail("'clearFlags' must be Skybox, SolidColor, Depth or Nothing.");
                camera.clearFlags = flags;
            }

            object rawColor;
            if (args.TryGetValue("backgroundColor", out rawColor) && rawColor != null)
            {
                Color color;
                if (!TryColor(rawColor, out color)) return Fail("'backgroundColor' must be [r,g,b], [r,g,b,a], or a colour name/hex.");
                camera.backgroundColor = color;
            }

            if (args.ContainsKey("orthographic")) camera.orthographic = Bool(args, "orthographic", false);
            if (args.ContainsKey("orthographicSize"))
            {
                camera.orthographicSize = (float)Num(args, camera.orthographicSize, "orthographicSize");
            }

            if (Bool(args, "main", false) || Bool(args, "mainCamera", false)) holder.tag = "MainCamera";

            if (Bool(args, "audioListener", true) && !SceneHasAudioListener(holder.scene))
            {
                AddComponentTo(holder, "UnityEngine.AudioListener", scope, out error);
            }

            MarkDirty(holder);
            Dictionary<string, object> data = Describe(holder);
            data["fieldOfView"] = camera.fieldOfView;
            data["clearFlags"] = camera.clearFlags.ToString();
            data["tag"] = holder.tag;
            return Ok("Created camera " + HierarchyPath(holder.transform) + ".", data);
        }

        private static bool SceneHasAudioListener(Scene scene)
        {
            foreach (AudioListener listener in Resources.FindObjectsOfTypeAll<AudioListener>())
            {
                if (listener != null && listener.gameObject.scene == scene) return true;
            }
            return false;
        }

        #endregion

        #region Object resolution

        /// <summary>The object CreateGameObject most recently made; a fallback for callers that lose the id.</summary>
        private static GameObject LastCreated;

        private static GameObject ResolveTarget(Dictionary<string, object> args, Scope scope, out string error)
        {
            // "path" is deliberately not an alias: SaveScene, CreatePrefab
            // and friends use it for an asset path, and one word meaning
            // two things is how a prefab ends up saved over a scene.
            string reference = Str(args, "target", "gameObject", "gameObjectPath", "object", "ref", "guid");
            return Resolve(reference, scope, out error);
        }

        /// <summary>
        /// Finds a GameObject from a reference: a hierarchy path
        /// ("Root/Child"), optionally scene-qualified
        /// ("Assets/Scenes/Main.unity::Root/Child"), "id:&lt;instanceId&gt;",
        /// "gid:&lt;GlobalObjectId&gt;", or a bare name searched anywhere.
        /// </summary>
        private static GameObject Resolve(string reference, Scope scope, out string error)
        {
            error = null;

            if (scope != null && scope.InPrefab)
            {
                if (string.IsNullOrEmpty(reference) || reference.Trim() == "/" || reference.Trim() == ".")
                {
                    return scope.PrefabRoot;
                }
                GameObject inside = FindUnder(scope.PrefabRoot.transform, reference.Trim().TrimStart('/'));
                if (inside == null) error = "No object '" + reference + "' inside prefab '" + scope.PrefabRoot.name + "'.";
                return inside;
            }

            if (string.IsNullOrWhiteSpace(reference))
            {
                error = "A target is required: a hierarchy path, id:<instanceId> or gid:<globalId>.";
                return null;
            }

            string text = reference.Trim();

            int instanceId;
            if (text.StartsWith("id:", StringComparison.OrdinalIgnoreCase) &&
                int.TryParse(text.Substring(3), out instanceId))
            {
                GameObject byId = AsGameObject(EditorUtility.InstanceIDToObject(instanceId));
                if (byId == null) error = "No object with instance id " + instanceId + " (ids last for one editor session).";
                return byId;
            }
            if (int.TryParse(text, out instanceId))
            {
                GameObject byId = AsGameObject(EditorUtility.InstanceIDToObject(instanceId));
                if (byId == null) error = "No object with instance id " + instanceId + ".";
                return byId;
            }

            if (text.StartsWith("gid:", StringComparison.OrdinalIgnoreCase))
            {
                GlobalObjectId globalId;
                if (!GlobalObjectId.TryParse(text.Substring(4), out globalId))
                {
                    error = "'" + text + "' is not a valid global object id.";
                    return null;
                }
                GameObject byGlobal = AsGameObject(GlobalObjectId.GlobalObjectIdentifierToObjectSlow(globalId));
                if (byGlobal == null) error = "No loaded object with global id " + text.Substring(4) + ".";
                return byGlobal;
            }

            string sceneFilter = null;
            string path = text;
            int separator = text.IndexOf("::", StringComparison.Ordinal);
            if (separator >= 0)
            {
                sceneFilter = text.Substring(0, separator).Trim();
                path = text.Substring(separator + 2);
            }
            path = path.Trim().TrimStart('/');

            for (int index = 0; index < SceneManager.sceneCount; index++)
            {
                Scene scene = SceneManager.GetSceneAt(index);
                if (!scene.isLoaded) continue;
                if (sceneFilter != null && !SceneMatches(scene, sceneFilter)) continue;

                GameObject byPath = FindInScene(scene, path);
                if (byPath != null) return byPath;
            }

            // A bare name is allowed to mean "wherever it is", inactive
            // objects included, because that is how people refer to things.
            if (path.IndexOf('/') < 0)
            {
                foreach (GameObject candidate in Resources.FindObjectsOfTypeAll<GameObject>())
                {
                    if (candidate == null || candidate.name != path) continue;
                    if (!candidate.scene.IsValid() || !candidate.scene.isLoaded) continue;
                    if (EditorSceneManager.IsPreviewScene(candidate.scene)) continue;
                    if (sceneFilter != null && !SceneMatches(candidate.scene, sceneFilter)) continue;
                    return candidate;
                }
            }

            error = sceneFilter != null
                ? "No object '" + path + "' in loaded scene '" + sceneFilter + "'."
                : "No object at hierarchy path '" + path + "' in any loaded scene.";
            return null;
        }

        private static bool SceneMatches(Scene scene, string wanted)
        {
            string normalised = wanted.Replace('\\', '/');
            string scenePath = scene.path ?? "";
            return string.Equals(scenePath, normalised, StringComparison.OrdinalIgnoreCase)
                || string.Equals(scene.name, normalised, StringComparison.OrdinalIgnoreCase)
                || string.Equals(scene.name + ".unity", normalised, StringComparison.OrdinalIgnoreCase)
                || scenePath.EndsWith("/" + normalised, StringComparison.OrdinalIgnoreCase);
        }

        private static GameObject FindInScene(Scene scene, string path)
        {
            if (string.IsNullOrEmpty(path)) return null;

            string first = path;
            string rest = null;
            int slash = path.IndexOf('/');
            if (slash >= 0)
            {
                first = path.Substring(0, slash);
                rest = path.Substring(slash + 1);
            }

            foreach (GameObject root in scene.GetRootGameObjects())
            {
                if (root.name != first) continue;
                if (string.IsNullOrEmpty(rest)) return root;
                Transform child = root.transform.Find(rest);
                if (child != null) return child.gameObject;
            }
            return null;
        }

        private static GameObject FindUnder(Transform root, string path)
        {
            if (path == root.name) return root.gameObject;

            string prefix = root.name + "/";
            if (path.StartsWith(prefix, StringComparison.Ordinal))
            {
                Transform qualified = root.Find(path.Substring(prefix.Length));
                if (qualified != null) return qualified.gameObject;
            }

            Transform direct = root.Find(path);
            return direct != null ? direct.gameObject : null;
        }

        private static GameObject AsGameObject(Object value)
        {
            GameObject asObject = value as GameObject;
            if (asObject != null) return asObject;
            Component asComponent = value as Component;
            return asComponent != null ? asComponent.gameObject : null;
        }

        /// <summary>
        /// The Object a SetField/GetField acts on: a component of the
        /// target, or the GameObject itself when componentType is absent
        /// or "GameObject".
        /// </summary>
        private static Object ResolveMemberOwner(Dictionary<string, object> args, Scope scope, out string error)
        {
            GameObject target = ResolveTarget(args, scope, out error);
            if (target == null) return null;

            string typeName = Str(args, "componentType", "component", "type");
            if (string.IsNullOrEmpty(typeName) || string.Equals(typeName, "GameObject", StringComparison.OrdinalIgnoreCase))
            {
                return target;
            }

            Type type = ResolveComponentType(typeName, out error);
            if (type == null) return null;

            Component component = target.GetComponent(type);
            if (component == null)
            {
                error = HierarchyPath(target.transform) + " has no " + type.Name + ".";
                return null;
            }
            return component;
        }

        private static Transform OwnerTransform(Object owner)
        {
            GameObject asObject = owner as GameObject;
            if (asObject != null) return asObject.transform;
            Component asComponent = owner as Component;
            return asComponent != null ? asComponent.transform : null;
        }

        #endregion

        #region Types and components

        private static readonly Dictionary<string, Type> TypeCache =
            new Dictionary<string, Type>(StringComparer.OrdinalIgnoreCase);

        /// <summary>
        /// A type by full or simple name, from any loaded assembly. Among
        /// several simple-name matches, a Component wins, and a Unity one
        /// wins over a user one; a request that means something else can
        /// say the full name.
        /// </summary>
        private static Type ResolveType(string typeName)
        {
            if (string.IsNullOrWhiteSpace(typeName)) return null;
            string wanted = typeName.Trim();

            Type cached;
            if (TypeCache.TryGetValue(wanted, out cached)) return cached;

            Type found = Type.GetType(wanted, false, true);

            if (found == null)
            {
                foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
                {
                    try { found = assembly.GetType(wanted, false, true); }
                    catch (Exception) { found = null; }
                    if (found != null) break;
                }
            }

            if (found == null)
            {
                int bestScore = 0;
                foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
                {
                    Type[] types;
                    try { types = assembly.GetTypes(); }
                    catch (ReflectionTypeLoadException partial) { types = partial.Types; }
                    catch (Exception) { continue; }

                    foreach (Type candidate in types)
                    {
                        if (candidate == null) continue;
                        if (!string.Equals(candidate.Name, wanted, StringComparison.OrdinalIgnoreCase)) continue;

                        int score = 1;
                        if (typeof(Component).IsAssignableFrom(candidate)) score = 2;
                        if (score == 2 && candidate.Namespace != null &&
                            candidate.Namespace.StartsWith("UnityEngine", StringComparison.Ordinal)) score = 3;

                        if (score > bestScore)
                        {
                            bestScore = score;
                            found = candidate;
                        }
                    }
                }
            }

            if (found != null) TypeCache[wanted] = found;
            return found;
        }

        private static Type ResolveComponentType(string typeName, out string error)
        {
            error = null;
            Type type = ResolveType(typeName);
            if (type == null)
            {
                error = "No type named '" + typeName + "' in any loaded assembly.";
                return null;
            }
            if (!typeof(Component).IsAssignableFrom(type))
            {
                error = "'" + type.FullName + "' is not a Component.";
                return null;
            }
            return type;
        }

        private static Component AddComponentTo(GameObject target, string typeName, Scope scope, out string error)
        {
            Type type = ResolveComponentType(typeName, out error);
            if (type == null) return null;

            try
            {
                Component added = scope.Undo ? Undo.AddComponent(target, type) : target.AddComponent(type);
                if (added == null)
                {
                    error = "Unity refused to add " + type.Name + " to " + target.name + ".";
                    return null;
                }
                return added;
            }
            catch (Exception failure)
            {
                error = "Adding " + type.Name + " to " + target.name + " failed: " + failure.Message;
                return null;
            }
        }

        private static void Destroy(GameObject target, Scope scope)
        {
            if (scope.Undo) Undo.DestroyObjectImmediate(target);
            else Object.DestroyImmediate(target);
        }

        private static void MarkDirty(GameObject target)
        {
            if (target == null) return;
            EditorUtility.SetDirty(target);
            if (target.scene.IsValid() && !EditorSceneManager.IsPreviewScene(target.scene))
            {
                EditorSceneManager.MarkSceneDirty(target.scene);
            }
        }

        #endregion

        #region Fields: serialized first, then reflection

        private static bool IsActiveAlias(string field)
        {
            string key = Normalise(field);
            return key == "active" || key == "isactive" || key == "activeself" || key == "enabled";
        }

        /// <summary>
        /// Lowercase, without Unity's m_ prefix or any spaces and
        /// underscores, so "mass", "m_Mass" and "Mass" all meet.
        /// </summary>
        private static string Normalise(string name)
        {
            if (name == null) return "";
            string trimmed = name.Trim();
            if (trimmed.StartsWith("m_", StringComparison.Ordinal)) trimmed = trimmed.Substring(2);
            StringBuilder builder = new StringBuilder(trimmed.Length);
            foreach (char character in trimmed)
            {
                if (character == ' ' || character == '_' || character == '-') continue;
                builder.Append(char.ToLowerInvariant(character));
            }
            return builder.ToString();
        }

        private static SerializedProperty FindSerialized(SerializedObject serialized, string field)
        {
            SerializedProperty direct = serialized.FindProperty(field);
            if (direct != null) return direct;

            string wanted = Normalise(field);
            SerializedProperty iterator = serialized.GetIterator();
            if (iterator.NextVisible(true))
            {
                do
                {
                    if (Normalise(iterator.name) == wanted || Normalise(iterator.displayName) == wanted)
                    {
                        return iterator.Copy();
                    }
                }
                while (iterator.NextVisible(false));
            }
            return null;
        }

        private static List<string> SerializedNames(SerializedObject serialized)
        {
            List<string> names = new List<string>();
            SerializedProperty iterator = serialized.GetIterator();
            if (iterator.NextVisible(true))
            {
                do
                {
                    if (iterator.name != "m_Script") names.Add(iterator.name);
                }
                while (iterator.NextVisible(false) && names.Count < 40);
            }
            return names;
        }

        private static bool SetMember(Object owner, string field, object value, Scope scope,
                                      out string written, out string error)
        {
            written = field;
            error = null;

            SerializedObject serialized = new SerializedObject(owner);
            SerializedProperty property = FindSerialized(serialized, field);
            if (property != null)
            {
                string serializedError;
                if (ApplySerialized(property, value, scope, out serializedError))
                {
                    serialized.ApplyModifiedProperties();
                    EditorUtility.SetDirty(owner);
                    written = property.propertyPath;
                    return true;
                }

                // A serialized int can be an enum in C# (Rigidbody's
                // m_Constraints is RigidbodyConstraints), and only the
                // C# side knows the names. Reflection gets a turn before
                // the serialized refusal stands.
                bool found;
                if (SetByReflection(owner, field, value, scope, out written, out error, out found)) return true;
                if (!found) error = serializedError;
                return false;
            }

            bool member;
            if (SetByReflection(owner, field, value, scope, out written, out error, out member)) return true;
            if (!member)
            {
                error = owner.GetType().Name + " has no settable field or property '" + field +
                        "'. Serialized fields: " + string.Join(", ", SerializedNames(serialized).ToArray()) + ".";
            }
            return false;
        }

        /// <summary>
        /// Sets a public property or field by name (exact, then normalised).
        /// `found` says whether such a member exists at all, so a caller
        /// can tell "no member" from "member refused the value".
        /// </summary>
        private static bool SetByReflection(Object owner, string field, object value, Scope scope,
                                            out string written, out string error, out bool found)
        {
            written = field;
            error = null;
            found = false;
            Type type = owner.GetType();

            PropertyInfo propertyInfo = FindPropertyInfo(type, field);
            if (propertyInfo != null && propertyInfo.CanWrite)
            {
                found = true;
                object converted;
                if (!ConvertValue(value, propertyInfo.PropertyType, scope, out converted, out error)) return false;
                if (scope.Undo) Undo.RecordObject(owner, "ARIA: set " + field);
                try
                {
                    propertyInfo.SetValue(owner, converted, null);
                }
                catch (TargetInvocationException failure)
                {
                    error = "Setting " + field + " failed: " + (failure.InnerException ?? failure).Message;
                    return false;
                }
                EditorUtility.SetDirty(owner);
                written = propertyInfo.Name;
                return true;
            }

            FieldInfo fieldInfo = FindFieldInfo(type, field);
            if (fieldInfo != null && !fieldInfo.IsInitOnly)
            {
                found = true;
                object converted;
                if (!ConvertValue(value, fieldInfo.FieldType, scope, out converted, out error)) return false;
                if (scope.Undo) Undo.RecordObject(owner, "ARIA: set " + field);
                fieldInfo.SetValue(owner, converted);
                EditorUtility.SetDirty(owner);
                written = fieldInfo.Name;
                return true;
            }

            return false;
        }

        private const BindingFlags MemberFlags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.IgnoreCase;

        private static PropertyInfo FindPropertyInfo(Type type, string field)
        {
            try
            {
                PropertyInfo exact = type.GetProperty(field, MemberFlags);
                if (exact != null) return exact;
            }
            catch (AmbiguousMatchException)
            {
                // Fall through to the scan, which takes the first match.
            }

            string wanted = Normalise(field);
            foreach (PropertyInfo candidate in type.GetProperties(MemberFlags))
            {
                if (candidate.GetIndexParameters().Length == 0 && Normalise(candidate.Name) == wanted) return candidate;
            }
            return null;
        }

        private static FieldInfo FindFieldInfo(Type type, string field)
        {
            FieldInfo exact = type.GetField(field, MemberFlags);
            if (exact != null) return exact;

            string wanted = Normalise(field);
            foreach (FieldInfo candidate in type.GetFields(MemberFlags))
            {
                if (Normalise(candidate.Name) == wanted) return candidate;
            }
            return null;
        }

        private static bool GetMember(Object owner, string field, out object value, out string error)
        {
            value = null;
            error = null;

            Type type = owner.GetType();

            // The C# surface first: "mass" is what a caller knows, and it
            // is what the inspector shows.
            PropertyInfo propertyInfo = FindPropertyInfo(type, field);
            if (propertyInfo != null && propertyInfo.CanRead && propertyInfo.GetIndexParameters().Length == 0)
            {
                try
                {
                    value = ToJsonValue(propertyInfo.GetValue(owner, null));
                    return true;
                }
                catch (TargetInvocationException failure)
                {
                    error = "Reading " + field + " failed: " + (failure.InnerException ?? failure).Message;
                    return false;
                }
            }

            FieldInfo fieldInfo = FindFieldInfo(type, field);
            if (fieldInfo != null)
            {
                value = ToJsonValue(fieldInfo.GetValue(owner));
                return true;
            }

            SerializedObject serialized = new SerializedObject(owner);
            SerializedProperty property = FindSerialized(serialized, field);
            if (property != null)
            {
                value = ReadSerialized(property);
                return true;
            }

            error = type.Name + " has no field or property '" + field + "'. Serialized fields: " +
                    string.Join(", ", SerializedNames(serialized).ToArray()) + ".";
            return false;
        }

        private static bool ApplySerialized(SerializedProperty property, object value, Scope scope, out string error)
        {
            error = null;
            double number;
            bool flag;

            switch (property.propertyType)
            {
                case SerializedPropertyType.Integer:
                case SerializedPropertyType.ArraySize:
                case SerializedPropertyType.Character:
                    if (value is string && property.propertyType == SerializedPropertyType.Character &&
                        ((string)value).Length == 1)
                    {
                        property.intValue = ((string)value)[0];
                        return true;
                    }
                    if (!TryNumber(value, out number)) return Wrong(property, "an integer", out error);
                    if (property.type == "long") property.longValue = (long)Math.Round(number);
                    else property.intValue = (int)Math.Round(number);
                    return true;

                case SerializedPropertyType.Boolean:
                    if (!TryBool(value, out flag)) return Wrong(property, "true or false", out error);
                    property.boolValue = flag;
                    return true;

                case SerializedPropertyType.Float:
                    if (!TryNumber(value, out number)) return Wrong(property, "a number", out error);
                    if (property.type == "double") property.doubleValue = number;
                    else property.floatValue = (float)number;
                    return true;

                case SerializedPropertyType.String:
                    property.stringValue = value == null ? "" : Text(value);
                    return true;

                case SerializedPropertyType.Color:
                    Color color;
                    if (!TryColor(value, out color)) return Wrong(property, "a colour ([r,g,b,a], name or hex)", out error);
                    property.colorValue = color;
                    return true;

                case SerializedPropertyType.ObjectReference:
                    Type expected = ExpectedReferenceType(property);
                    object reference;
                    if (!ConvertValue(value, expected, scope, out reference, out error)) return false;
                    property.objectReferenceValue = reference as Object;
                    return true;

                case SerializedPropertyType.LayerMask:
                    if (TryNumber(value, out number))
                    {
                        property.intValue = (int)Math.Round(number);
                        return true;
                    }
                    if (value is string)
                    {
                        property.intValue = LayerMask.GetMask(((string)value).Split(','));
                        return true;
                    }
                    return Wrong(property, "a layer mask number or layer names", out error);

                case SerializedPropertyType.Enum:
                    if (value is string)
                    {
                        string wanted = Normalise((string)value);
                        string[] names = property.enumNames;
                        string[] display = property.enumDisplayNames;
                        for (int index = 0; index < names.Length; index++)
                        {
                            if (Normalise(names[index]) == wanted ||
                                (display != null && index < display.Length && Normalise(display[index]) == wanted))
                            {
                                property.enumValueIndex = index;
                                return true;
                            }
                        }
                        error = "'" + value + "' is not one of " + string.Join(", ", names) + ".";
                        return false;
                    }
                    if (!TryNumber(value, out number)) return Wrong(property, "an enum name or number", out error);
                    property.intValue = (int)Math.Round(number);
                    return true;

                case SerializedPropertyType.Vector2:
                    Vector2 vector2;
                    if (!TryVector2(value, out vector2)) return Wrong(property, "[x,y]", out error);
                    property.vector2Value = vector2;
                    return true;

                case SerializedPropertyType.Vector3:
                    Vector3 vector3;
                    if (!TryVector3(value, out vector3)) return Wrong(property, "[x,y,z]", out error);
                    property.vector3Value = vector3;
                    return true;

                case SerializedPropertyType.Vector4:
                    Vector4 vector4;
                    if (!TryVector4(value, out vector4)) return Wrong(property, "[x,y,z,w]", out error);
                    property.vector4Value = vector4;
                    return true;

                case SerializedPropertyType.Vector2Int:
                    Vector2 int2;
                    if (!TryVector2(value, out int2)) return Wrong(property, "[x,y]", out error);
                    property.vector2IntValue = new Vector2Int(Mathf.RoundToInt(int2.x), Mathf.RoundToInt(int2.y));
                    return true;

                case SerializedPropertyType.Vector3Int:
                    Vector3 int3;
                    if (!TryVector3(value, out int3)) return Wrong(property, "[x,y,z]", out error);
                    property.vector3IntValue = new Vector3Int(Mathf.RoundToInt(int3.x), Mathf.RoundToInt(int3.y), Mathf.RoundToInt(int3.z));
                    return true;

                case SerializedPropertyType.Quaternion:
                    Quaternion rotation;
                    if (!TryQuaternion(value, out rotation)) return Wrong(property, "euler [x,y,z] or [x,y,z,w]", out error);
                    property.quaternionValue = rotation;
                    return true;

                case SerializedPropertyType.Rect:
                    List<double> rect;
                    if (!TryNumbers(value, out rect) || rect.Count != 4) return Wrong(property, "[x,y,width,height]", out error);
                    property.rectValue = new Rect((float)rect[0], (float)rect[1], (float)rect[2], (float)rect[3]);
                    return true;

                case SerializedPropertyType.Bounds:
                    Dictionary<string, object> bounds = value as Dictionary<string, object>;
                    Vector3 center, size;
                    if (bounds == null || !bounds.ContainsKey("center") || !bounds.ContainsKey("size") ||
                        !TryVector3(bounds["center"], out center) || !TryVector3(bounds["size"], out size))
                    {
                        return Wrong(property, "{center:[x,y,z], size:[x,y,z]}", out error);
                    }
                    property.boundsValue = new Bounds(center, size);
                    return true;

                default:
                    error = "Field '" + property.propertyPath + "' is a " + property.propertyType +
                            ", which this bridge cannot set from JSON. Set its sub-fields by path (e.g. '" +
                            property.propertyPath + ".x') instead.";
                    return false;
            }
        }

        private static bool Wrong(SerializedProperty property, string expected, out string error)
        {
            error = "Field '" + property.propertyPath + "' needs " + expected + ".";
            return false;
        }

        /// <summary>The Object subtype a PPtr property holds, from its "PPtr&lt;$Name&gt;" type string.</summary>
        private static Type ExpectedReferenceType(SerializedProperty property)
        {
            string typeText = property.type ?? "";
            int start = typeText.IndexOf('<');
            int end = typeText.LastIndexOf('>');
            if (start >= 0 && end > start)
            {
                string name = typeText.Substring(start + 1, end - start - 1).TrimStart('$');
                Type resolved = ResolveType(name);
                if (resolved != null && typeof(Object).IsAssignableFrom(resolved)) return resolved;
            }
            return typeof(Object);
        }

        private static object ReadSerialized(SerializedProperty property)
        {
            switch (property.propertyType)
            {
                case SerializedPropertyType.Integer:
                case SerializedPropertyType.ArraySize:
                case SerializedPropertyType.Character:
                case SerializedPropertyType.LayerMask:
                    return property.type == "long" ? (object)property.longValue : property.intValue;
                case SerializedPropertyType.Boolean: return property.boolValue;
                case SerializedPropertyType.Float:
                    return property.type == "double" ? property.doubleValue : (double)property.floatValue;
                case SerializedPropertyType.String: return property.stringValue;
                case SerializedPropertyType.Color: return ToJsonValue(property.colorValue);
                case SerializedPropertyType.ObjectReference: return ToJsonValue(property.objectReferenceValue);
                case SerializedPropertyType.Enum:
                    string[] names = property.enumNames;
                    int index = property.enumValueIndex;
                    return index >= 0 && index < names.Length ? names[index] : (object)property.intValue;
                case SerializedPropertyType.Vector2: return ToJsonValue(property.vector2Value);
                case SerializedPropertyType.Vector3: return ToJsonValue(property.vector3Value);
                case SerializedPropertyType.Vector4: return ToJsonValue(property.vector4Value);
                case SerializedPropertyType.Vector2Int: return ToJsonValue((Vector2)property.vector2IntValue);
                case SerializedPropertyType.Vector3Int: return ToJsonValue((Vector3)property.vector3IntValue);
                case SerializedPropertyType.Quaternion: return ToJsonValue(property.quaternionValue);
                case SerializedPropertyType.Rect: return ToJsonValue(property.rectValue);
                case SerializedPropertyType.Bounds: return ToJsonValue(property.boundsValue);
                default: return "<" + property.propertyType + ">";
            }
        }

        /// <summary>Turns JSON into the CLR type a property or field wants.</summary>
        private static bool ConvertValue(object value, Type target, Scope scope, out object result, out string error)
        {
            result = null;
            error = null;
            double number;
            bool flag;

            if (target == typeof(string))
            {
                result = value == null ? null : Text(value);
                return true;
            }
            if (target == typeof(bool))
            {
                if (!TryBool(value, out flag)) return Cannot(value, target, out error);
                result = flag;
                return true;
            }
            if (target.IsEnum)
            {
                if (value is string)
                {
                    try
                    {
                        result = Enum.Parse(target, (string)value, true);
                        return true;
                    }
                    catch (ArgumentException)
                    {
                        error = "'" + value + "' is not one of " + string.Join(", ", Enum.GetNames(target)) + ".";
                        return false;
                    }
                }
                if (!TryNumber(value, out number)) return Cannot(value, target, out error);
                result = Enum.ToObject(target, (long)Math.Round(number));
                return true;
            }
            if (IsNumeric(target))
            {
                if (!TryNumber(value, out number)) return Cannot(value, target, out error);
                result = Convert.ChangeType(number, target, CultureInfo.InvariantCulture);
                return true;
            }
            if (target == typeof(Vector2))
            {
                Vector2 vector;
                if (!TryVector2(value, out vector)) return Cannot(value, target, out error);
                result = vector;
                return true;
            }
            if (target == typeof(Vector3))
            {
                Vector3 vector;
                if (!TryVector3(value, out vector)) return Cannot(value, target, out error);
                result = vector;
                return true;
            }
            if (target == typeof(Vector4))
            {
                Vector4 vector;
                if (!TryVector4(value, out vector)) return Cannot(value, target, out error);
                result = vector;
                return true;
            }
            if (target == typeof(Quaternion))
            {
                Quaternion rotation;
                if (!TryQuaternion(value, out rotation)) return Cannot(value, target, out error);
                result = rotation;
                return true;
            }
            if (target == typeof(Color))
            {
                Color color;
                if (!TryColor(value, out color)) return Cannot(value, target, out error);
                result = color;
                return true;
            }
            if (target == typeof(LayerMask))
            {
                if (TryNumber(value, out number))
                {
                    result = (LayerMask)(int)Math.Round(number);
                    return true;
                }
                if (value is string)
                {
                    result = (LayerMask)LayerMask.GetMask(((string)value).Split(','));
                    return true;
                }
                return Cannot(value, target, out error);
            }
            if (target == typeof(Rect))
            {
                List<double> numbers;
                if (!TryNumbers(value, out numbers) || numbers.Count != 4) return Cannot(value, target, out error);
                result = new Rect((float)numbers[0], (float)numbers[1], (float)numbers[2], (float)numbers[3]);
                return true;
            }
            if (typeof(Object).IsAssignableFrom(target))
            {
                Object reference;
                if (!ResolveReference(value, target, scope, out reference, out error)) return false;
                result = reference;
                return true;
            }

            error = "This bridge cannot build a " + target.Name + " from JSON.";
            return false;
        }

        private static bool Cannot(object value, Type target, out string error)
        {
            error = "Cannot turn " + Json.Serialize(value, false) + " into a " + target.Name + ".";
            return false;
        }

        /// <summary>
        /// An Object from JSON: null, an instance id, an asset path
        /// ("Assets/..." or "Packages/..."), a scene reference (as Resolve
        /// takes), or a {path|assetPath|instanceId} object as GetField
        /// reports one.
        /// </summary>
        private static bool ResolveReference(object value, Type target, Scope scope, out Object result, out string error)
        {
            result = null;
            error = null;

            if (value == null) return true;

            Dictionary<string, object> described = value as Dictionary<string, object>;
            if (described != null)
            {
                object inner;
                if (described.TryGetValue("assetPath", out inner) && inner is string && !string.IsNullOrEmpty((string)inner))
                {
                    return ResolveReference(inner, target, scope, out result, out error);
                }
                if (described.TryGetValue("path", out inner) && inner is string && !string.IsNullOrEmpty((string)inner))
                {
                    return ResolveReference(inner, target, scope, out result, out error);
                }
                if (described.TryGetValue("instanceId", out inner))
                {
                    return ResolveReference(inner, target, scope, out result, out error);
                }
                error = "An object reference needs assetPath, path or instanceId.";
                return false;
            }

            double number;
            if (TryNumber(value, out number) && !(value is string))
            {
                Object byId = EditorUtility.InstanceIDToObject((int)Math.Round(number));
                if (byId == null)
                {
                    error = "No object with instance id " + (int)Math.Round(number) + ".";
                    return false;
                }
                return Narrow(byId, target, out result, out error);
            }

            string text = value as string;
            if (text == null)
            {
                error = "An object reference must be a string, a number or null.";
                return false;
            }
            text = text.Trim();
            if (text.Length == 0) return true;

            if (text.StartsWith("Assets/", StringComparison.Ordinal) || text.StartsWith("Packages/", StringComparison.Ordinal))
            {
                Object asset = AssetDatabase.LoadAssetAtPath(text, target);
                if (asset == null && typeof(Component).IsAssignableFrom(target))
                {
                    GameObject prefab = AssetDatabase.LoadAssetAtPath<GameObject>(text);
                    if (prefab != null) asset = prefab.GetComponent(target);
                }
                if (asset == null && target == typeof(Object))
                {
                    asset = AssetDatabase.LoadMainAssetAtPath(text);
                }
                if (asset == null)
                {
                    error = "No " + target.Name + " asset at " + text + ".";
                    return false;
                }
                result = asset;
                return true;
            }

            GameObject inScene = Resolve(text, scope, out error);
            if (inScene == null) return false;
            return Narrow(inScene, target, out result, out error);
        }

        private static bool Narrow(Object candidate, Type target, out Object result, out string error)
        {
            result = null;
            error = null;

            if (target.IsInstanceOfType(candidate))
            {
                result = candidate;
                return true;
            }

            GameObject asObject = AsGameObject(candidate);
            if (asObject != null)
            {
                if (target == typeof(GameObject) || target == typeof(Object))
                {
                    result = asObject;
                    return true;
                }
                if (typeof(Component).IsAssignableFrom(target))
                {
                    Component component = asObject.GetComponent(target);
                    if (component != null)
                    {
                        result = component;
                        return true;
                    }
                    error = HierarchyPath(asObject.transform) + " has no " + target.Name + ".";
                    return false;
                }
            }

            error = candidate.GetType().Name + " is not a " + target.Name + ".";
            return false;
        }

        private static bool IsNumeric(Type type)
        {
            return type == typeof(int) || type == typeof(float) || type == typeof(double) ||
                   type == typeof(long) || type == typeof(short) || type == typeof(byte) ||
                   type == typeof(uint) || type == typeof(ulong) || type == typeof(ushort) ||
                   type == typeof(sbyte) || type == typeof(decimal);
        }

        #endregion

        #region Transforms and descriptions

        /// <summary>Applies any of position/rotation/scale present in args.</summary>
        private static bool ApplyTransform(Transform transform, Dictionary<string, object> args, bool local, out string error)
        {
            error = null;
            object raw;

            if (args.TryGetValue("position", out raw) && raw != null)
            {
                Vector3 position;
                if (!TryVector3(raw, out position))
                {
                    error = "'position' must be [x,y,z].";
                    return false;
                }
                if (local) transform.localPosition = position;
                else transform.position = position;
            }

            if (args.TryGetValue("rotation", out raw) && raw != null)
            {
                Quaternion rotation;
                if (!TryQuaternion(raw, out rotation))
                {
                    error = "'rotation' must be euler [x,y,z] or a quaternion [x,y,z,w].";
                    return false;
                }
                if (local) transform.localRotation = rotation;
                else transform.rotation = rotation;
            }

            if (args.TryGetValue("scale", out raw) && raw != null)
            {
                Vector3 scale;
                double uniform;
                if (TryNumber(raw, out uniform) && !(raw is string))
                {
                    scale = Vector3.one * (float)uniform;
                }
                else if (!TryVector3(raw, out scale))
                {
                    error = "'scale' must be [x,y,z] or a single number.";
                    return false;
                }
                transform.localScale = scale;
            }

            return true;
        }

        private static string HierarchyPath(Transform transform)
        {
            if (transform == null) return "";
            List<string> parts = new List<string>();
            while (transform != null)
            {
                parts.Insert(0, transform.name);
                transform = transform.parent;
            }
            return string.Join("/", parts.ToArray());
        }

        private static Dictionary<string, object> Describe(GameObject target)
        {
            LastCreated = target;
            Dictionary<string, object> data = new Dictionary<string, object>();
            data["name"] = target.name;
            data["path"] = HierarchyPath(target.transform);
            data["instanceId"] = target.GetInstanceID();
            data["globalId"] = GlobalObjectId.GetGlobalObjectIdSlow(target).ToString();
            data["scene"] = target.scene.IsValid() ? target.scene.name : "";
            data["active"] = target.activeSelf;
            return data;
        }

        private static Dictionary<string, object> DescribeComponent(Component component)
        {
            Dictionary<string, object> data = new Dictionary<string, object>();
            data["path"] = HierarchyPath(component.transform);
            data["component"] = component.GetType().Name;
            data["componentInstanceId"] = component.GetInstanceID();
            return data;
        }

        private static Dictionary<string, object> DescribeTransform(Transform transform)
        {
            Dictionary<string, object> data = new Dictionary<string, object>();
            data["path"] = HierarchyPath(transform);
            data["position"] = ToJsonValue(transform.position);
            data["rotation"] = ToJsonValue(transform.eulerAngles);
            data["localPosition"] = ToJsonValue(transform.localPosition);
            data["localRotation"] = ToJsonValue(transform.localEulerAngles);
            data["scale"] = ToJsonValue(transform.localScale);
            return data;
        }

        private static void Describe(GameObject target, int depth, int maxDepth, List<object> into)
        {
            if (target == null) return;
            if (maxDepth >= 0 && depth > maxDepth) return;

            Dictionary<string, object> described = new Dictionary<string, object>();
            described["name"] = target.name;
            described["path"] = HierarchyPath(target.transform);
            described["instanceId"] = target.GetInstanceID();
            described["active"] = target.activeInHierarchy;
            described["depth"] = depth;

            List<object> components = new List<object>();
            foreach (Component component in target.GetComponents<Component>())
            {
                // A missing script is null here; saying so beats skipping
                // it, since a broken reference is what someone would ask about.
                components.Add(component == null ? "<missing script>" : component.GetType().Name);
            }
            described["components"] = components;
            into.Add(described);

            foreach (Transform child in target.transform) Describe(child.gameObject, depth + 1, maxDepth, into);
        }

        #endregion

        #region Arguments

        private static string Str(Dictionary<string, object> args, params string[] keys)
        {
            foreach (string key in keys)
            {
                object value;
                if (args.TryGetValue(key, out value) && value != null)
                {
                    return Text(value);
                }
            }
            return null;
        }

        private static string Text(object value)
        {
            if (value == null) return "";
            if (value is double) return ((double)value).ToString("R", CultureInfo.InvariantCulture);
            if (value is bool) return (bool)value ? "true" : "false";
            if (value is string) return (string)value;
            if (value is IList || value is IDictionary) return Json.Serialize(value, false);
            return Convert.ToString(value, CultureInfo.InvariantCulture);
        }

        private static bool Bool(Dictionary<string, object> args, string key, bool fallback)
        {
            object value;
            bool parsed;
            if (args.TryGetValue(key, out value) && TryBool(value, out parsed)) return parsed;
            return fallback;
        }

        private static double Num(Dictionary<string, object> args, double fallback, params string[] keys)
        {
            foreach (string key in keys)
            {
                object value;
                double parsed;
                if (args.TryGetValue(key, out value) && TryNumber(value, out parsed)) return parsed;
            }
            return fallback;
        }

        private static bool TryEnum<T>(string text, out T value) where T : struct
        {
            value = default(T);
            if (string.IsNullOrEmpty(text)) return false;
            string wanted = Normalise(text);
            foreach (string name in Enum.GetNames(typeof(T)))
            {
                if (Normalise(name) == wanted)
                {
                    value = (T)Enum.Parse(typeof(T), name);
                    return true;
                }
            }
            return false;
        }

        private static bool TryNumber(object value, out double number)
        {
            number = 0;
            if (value == null) return false;
            if (value is double) { number = (double)value; return true; }
            if (value is float) { number = (float)value; return true; }
            if (value is int) { number = (int)value; return true; }
            if (value is long) { number = (long)value; return true; }
            if (value is bool) { number = (bool)value ? 1 : 0; return true; }
            string text = value as string;
            return text != null && double.TryParse(text.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out number);
        }

        private static bool TryBool(object value, out bool flag)
        {
            flag = false;
            if (value is bool) { flag = (bool)value; return true; }
            double number;
            if (value is double) { flag = (double)value != 0; return true; }
            string text = value as string;
            if (text == null) return false;
            switch (text.Trim().ToLowerInvariant())
            {
                case "true": case "yes": case "on": case "1": flag = true; return true;
                case "false": case "no": case "off": case "0": flag = false; return true;
            }
            if (TryNumber(text, out number)) { flag = number != 0; return true; }
            return false;
        }

        /// <summary>
        /// A list of numbers from a JSON array, a {x,y,z,w} / {r,g,b,a}
        /// object, or a "1, 2, 3" string.
        /// </summary>
        private static bool TryNumbers(object value, out List<double> numbers)
        {
            numbers = new List<double>();
            if (value == null) return false;

            IList list = value as IList;
            if (list != null)
            {
                foreach (object item in list)
                {
                    double number;
                    if (!TryNumber(item, out number)) return false;
                    numbers.Add(number);
                }
                return numbers.Count > 0;
            }

            Dictionary<string, object> map = value as Dictionary<string, object>;
            if (map != null)
            {
                string[][] layouts = { new[] { "x", "y", "z", "w" }, new[] { "r", "g", "b", "a" } };
                foreach (string[] layout in layouts)
                {
                    if (!map.ContainsKey(layout[0])) continue;
                    foreach (string key in layout)
                    {
                        object item;
                        double number;
                        if (!map.TryGetValue(key, out item)) break;
                        if (!TryNumber(item, out number)) return false;
                        numbers.Add(number);
                    }
                    return numbers.Count > 0;
                }
                return false;
            }

            string text = value as string;
            if (text != null)
            {
                foreach (string part in text.Split(','))
                {
                    double number;
                    if (!TryNumber(part, out number)) return false;
                    numbers.Add(number);
                }
                return numbers.Count > 0;
            }

            return false;
        }

        private static bool TryVector2(object value, out Vector2 vector)
        {
            vector = Vector2.zero;
            List<double> numbers;
            if (!TryNumbers(value, out numbers) || numbers.Count < 2) return false;
            vector = new Vector2((float)numbers[0], (float)numbers[1]);
            return true;
        }

        private static bool TryVector3(object value, out Vector3 vector)
        {
            vector = Vector3.zero;
            List<double> numbers;
            if (!TryNumbers(value, out numbers) || numbers.Count < 3) return false;
            vector = new Vector3((float)numbers[0], (float)numbers[1], (float)numbers[2]);
            return true;
        }

        private static bool TryVector4(object value, out Vector4 vector)
        {
            vector = Vector4.zero;
            List<double> numbers;
            if (!TryNumbers(value, out numbers) || numbers.Count < 4) return false;
            vector = new Vector4((float)numbers[0], (float)numbers[1], (float)numbers[2], (float)numbers[3]);
            return true;
        }

        /// <summary>Three numbers are euler degrees; four are a quaternion.</summary>
        private static bool TryQuaternion(object value, out Quaternion rotation)
        {
            rotation = Quaternion.identity;
            List<double> numbers;
            if (!TryNumbers(value, out numbers)) return false;
            if (numbers.Count == 3)
            {
                rotation = Quaternion.Euler((float)numbers[0], (float)numbers[1], (float)numbers[2]);
                return true;
            }
            if (numbers.Count == 4)
            {
                rotation = new Quaternion((float)numbers[0], (float)numbers[1], (float)numbers[2], (float)numbers[3]);
                return true;
            }
            return false;
        }

        /// <summary>
        /// [r,g,b] or [r,g,b,a] in 0..1 (or 0..255 if any part exceeds 1),
        /// or anything ColorUtility reads: "#ff8800", "red".
        /// </summary>
        private static bool TryColor(object value, out Color color)
        {
            color = Color.white;
            string text = value as string;
            if (text != null && !text.Contains(","))
            {
                return ColorUtility.TryParseHtmlString(text.Trim(), out color);
            }

            List<double> numbers;
            if (!TryNumbers(value, out numbers) || numbers.Count < 3 || numbers.Count > 4) return false;

            float divisor = 1f;
            foreach (double part in numbers)
            {
                if (part > 1.0) divisor = 255f;
            }

            color = new Color((float)numbers[0] / divisor, (float)numbers[1] / divisor, (float)numbers[2] / divisor,
                              numbers.Count == 4 ? (float)numbers[3] / divisor : 1f);
            return true;
        }

        /// <summary>Whether an asset path is under Assets/, free of traversal, and ends as required.</summary>
        private static bool SafeAssetPath(string path, string suffix, out string error)
        {
            error = null;
            if (string.IsNullOrWhiteSpace(path))
            {
                error = "An asset path is required.";
                return false;
            }

            string normalised = path.Replace('\\', '/').Trim();
            if (normalised.Contains(".."))
            {
                error = "Asset paths must not contain '..'.";
                return false;
            }
            if (Path.IsPathRooted(normalised))
            {
                error = "Asset paths must be relative to the project, like Assets/Scenes/Main.unity.";
                return false;
            }
            if (!normalised.StartsWith("Assets/", StringComparison.Ordinal))
            {
                error = "Asset paths must start with Assets/.";
                return false;
            }
            if (!string.IsNullOrEmpty(suffix) && !normalised.EndsWith(suffix, StringComparison.OrdinalIgnoreCase))
            {
                error = "This path must end with " + suffix + ".";
                return false;
            }
            return true;
        }

        private static void EnsureFolder(string assetPath)
        {
            string folder = Path.GetDirectoryName(assetPath);
            if (string.IsNullOrEmpty(folder)) return;

            folder = folder.Replace('\\', '/');
            if (AssetDatabase.IsValidFolder(folder)) return;

            string[] parts = folder.Split('/');
            string built = parts[0];
            for (int index = 1; index < parts.Length; index++)
            {
                string next = built + "/" + parts[index];
                if (!AssetDatabase.IsValidFolder(next)) AssetDatabase.CreateFolder(built, parts[index]);
                built = next;
            }
        }

        /// <summary>A CLR value as something the JSON writer understands.</summary>
        private static object ToJsonValue(object value)
        {
            if (value == null) return null;
            if (value is string || value is bool || value is double) return value;
            if (value is float) return (double)(float)value;
            if (value is int || value is long || value is short || value is byte ||
                value is uint || value is ushort || value is sbyte)
            {
                return Convert.ToDouble(value, CultureInfo.InvariantCulture);
            }
            if (value is ulong || value is decimal) return Convert.ToDouble(value, CultureInfo.InvariantCulture);
            if (value is Enum) return value.ToString();
            if (value is Vector2) { Vector2 v = (Vector2)value; return new List<object> { (double)v.x, (double)v.y }; }
            if (value is Vector3) { Vector3 v = (Vector3)value; return new List<object> { (double)v.x, (double)v.y, (double)v.z }; }
            if (value is Vector4) { Vector4 v = (Vector4)value; return new List<object> { (double)v.x, (double)v.y, (double)v.z, (double)v.w }; }
            if (value is Vector2Int) { Vector2Int v = (Vector2Int)value; return new List<object> { (double)v.x, (double)v.y }; }
            if (value is Vector3Int) { Vector3Int v = (Vector3Int)value; return new List<object> { (double)v.x, (double)v.y, (double)v.z }; }
            if (value is Quaternion)
            {
                Quaternion q = (Quaternion)value;
                Dictionary<string, object> described = new Dictionary<string, object>();
                described["x"] = (double)q.x;
                described["y"] = (double)q.y;
                described["z"] = (double)q.z;
                described["w"] = (double)q.w;
                described["euler"] = ToJsonValue(q.eulerAngles);
                return described;
            }
            if (value is Color) { Color c = (Color)value; return new List<object> { (double)c.r, (double)c.g, (double)c.b, (double)c.a }; }
            if (value is Color32) return ToJsonValue((Color)(Color32)value);
            if (value is Rect)
            {
                Rect r = (Rect)value;
                return new List<object> { (double)r.x, (double)r.y, (double)r.width, (double)r.height };
            }
            if (value is Bounds)
            {
                Bounds b = (Bounds)value;
                Dictionary<string, object> described = new Dictionary<string, object>();
                described["center"] = ToJsonValue(b.center);
                described["size"] = ToJsonValue(b.size);
                return described;
            }
            if (value is LayerMask) return (double)((LayerMask)value).value;

            Object unityObject = value as Object;
            if (!ReferenceEquals(unityObject, null))
            {
                if (unityObject == null) return null; // destroyed
                Dictionary<string, object> described = new Dictionary<string, object>();
                described["name"] = unityObject.name;
                described["type"] = unityObject.GetType().Name;
                described["instanceId"] = (double)unityObject.GetInstanceID();
                string assetPath = AssetDatabase.GetAssetPath(unityObject);
                if (!string.IsNullOrEmpty(assetPath)) described["assetPath"] = assetPath;
                Transform transform = OwnerTransform(unityObject);
                if (transform != null) described["path"] = HierarchyPath(transform);
                return described;
            }

            IDictionary map = value as IDictionary;
            if (map != null)
            {
                Dictionary<string, object> converted = new Dictionary<string, object>();
                foreach (DictionaryEntry entry in map) converted[Convert.ToString(entry.Key, CultureInfo.InvariantCulture)] = ToJsonValue(entry.Value);
                return converted;
            }

            IEnumerable sequence = value as IEnumerable;
            if (sequence != null)
            {
                List<object> converted = new List<object>();
                foreach (object item in sequence) converted.Add(ToJsonValue(item));
                return converted;
            }

            return value.ToString();
        }

        #endregion

        #region JSON

        /// <summary>
        /// A small JSON reader and writer. JsonUtility cannot carry a
        /// dictionary or a value whose type is not known in advance, and
        /// a command's args are exactly that, so the bridge brings its own.
        /// Objects become Dictionary&lt;string, object&gt;, arrays become
        /// List&lt;object&gt;, numbers become double.
        /// </summary>
        private static class Json
        {
            public static bool TryParse(string text, out object value, out string error)
            {
                value = null;
                error = null;
                try
                {
                    Reader reader = new Reader(text);
                    value = reader.ReadValue();
                    reader.SkipWhitespace();
                    if (!reader.AtEnd) throw new FormatException("unexpected text after the JSON value at " + reader.Position);
                    return true;
                }
                catch (Exception failure)
                {
                    error = failure.Message;
                    return false;
                }
            }

            public static string Serialize(object value, bool pretty)
            {
                StringBuilder builder = new StringBuilder();
                Write(builder, ToJsonValue(value), pretty ? 0 : -1);
                return builder.ToString();
            }

            private sealed class Reader
            {
                private readonly string _text;
                private int _index;

                public Reader(string text)
                {
                    _text = text ?? "";
                }

                public int Position { get { return _index; } }
                public bool AtEnd { get { return _index >= _text.Length; } }

                public void SkipWhitespace()
                {
                    while (_index < _text.Length && char.IsWhiteSpace(_text[_index])) _index++;
                }

                private char Peek()
                {
                    if (AtEnd) throw new FormatException("unexpected end of JSON");
                    return _text[_index];
                }

                private void Expect(char expected)
                {
                    SkipWhitespace();
                    if (Peek() != expected) throw new FormatException("expected '" + expected + "' at " + _index);
                    _index++;
                }

                public object ReadValue()
                {
                    SkipWhitespace();
                    char next = Peek();
                    switch (next)
                    {
                        case '{': return ReadObject();
                        case '[': return ReadArray();
                        case '"': return ReadString();
                        case 't': ReadWord("true"); return true;
                        case 'f': ReadWord("false"); return false;
                        case 'n': ReadWord("null"); return null;
                        default:
                            if (next == '-' || char.IsDigit(next)) return ReadNumber();
                            throw new FormatException("unexpected '" + next + "' at " + _index);
                    }
                }

                private void ReadWord(string word)
                {
                    if (string.CompareOrdinal(_text, _index, word, 0, word.Length) != 0)
                    {
                        throw new FormatException("bad literal at " + _index);
                    }
                    _index += word.Length;
                }

                private Dictionary<string, object> ReadObject()
                {
                    Dictionary<string, object> result = new Dictionary<string, object>();
                    _index++;
                    SkipWhitespace();
                    if (Peek() == '}')
                    {
                        _index++;
                        return result;
                    }

                    while (true)
                    {
                        SkipWhitespace();
                        if (Peek() != '"') throw new FormatException("expected a string key at " + _index);
                        string key = ReadString();
                        Expect(':');
                        result[key] = ReadValue();
                        SkipWhitespace();
                        char next = Peek();
                        _index++;
                        if (next == ',') continue;
                        if (next == '}') return result;
                        throw new FormatException("expected ',' or '}' at " + (_index - 1));
                    }
                }

                private List<object> ReadArray()
                {
                    List<object> result = new List<object>();
                    _index++;
                    SkipWhitespace();
                    if (Peek() == ']')
                    {
                        _index++;
                        return result;
                    }

                    while (true)
                    {
                        result.Add(ReadValue());
                        SkipWhitespace();
                        char next = Peek();
                        _index++;
                        if (next == ',') continue;
                        if (next == ']') return result;
                        throw new FormatException("expected ',' or ']' at " + (_index - 1));
                    }
                }

                private string ReadString()
                {
                    _index++;
                    StringBuilder builder = new StringBuilder();
                    while (true)
                    {
                        char next = Peek();
                        _index++;
                        if (next == '"') return builder.ToString();
                        if (next != '\\')
                        {
                            builder.Append(next);
                            continue;
                        }

                        char escaped = Peek();
                        _index++;
                        switch (escaped)
                        {
                            case '"': builder.Append('"'); break;
                            case '\\': builder.Append('\\'); break;
                            case '/': builder.Append('/'); break;
                            case 'b': builder.Append('\b'); break;
                            case 'f': builder.Append('\f'); break;
                            case 'n': builder.Append('\n'); break;
                            case 'r': builder.Append('\r'); break;
                            case 't': builder.Append('\t'); break;
                            case 'u':
                                if (_index + 4 > _text.Length) throw new FormatException("truncated \\u escape");
                                builder.Append((char)Convert.ToInt32(_text.Substring(_index, 4), 16));
                                _index += 4;
                                break;
                            default:
                                throw new FormatException("bad escape '\\" + escaped + "' at " + (_index - 1));
                        }
                    }
                }

                private object ReadNumber()
                {
                    int start = _index;
                    if (Peek() == '-') _index++;
                    while (!AtEnd)
                    {
                        char next = _text[_index];
                        if (char.IsDigit(next) || next == '.' || next == 'e' || next == 'E' || next == '+' || next == '-')
                        {
                            _index++;
                            continue;
                        }
                        break;
                    }

                    string token = _text.Substring(start, _index - start);
                    double number;
                    if (!double.TryParse(token, NumberStyles.Float, CultureInfo.InvariantCulture, out number))
                    {
                        throw new FormatException("bad number '" + token + "' at " + start);
                    }
                    return number;
                }
            }

            private static void Write(StringBuilder builder, object value, int indent)
            {
                if (value == null)
                {
                    builder.Append("null");
                    return;
                }

                string text = value as string;
                if (text != null)
                {
                    WriteString(builder, text);
                    return;
                }

                if (value is bool)
                {
                    builder.Append((bool)value ? "true" : "false");
                    return;
                }

                if (value is double)
                {
                    double number = (double)value;
                    if (double.IsNaN(number) || double.IsInfinity(number)) builder.Append("null");
                    else if (Math.Abs(number) < 1e15 && number == Math.Floor(number)) builder.Append(((long)number).ToString(CultureInfo.InvariantCulture));
                    else builder.Append(number.ToString("R", CultureInfo.InvariantCulture));
                    return;
                }

                IDictionary map = value as IDictionary;
                if (map != null)
                {
                    builder.Append('{');
                    bool first = true;
                    foreach (DictionaryEntry entry in map)
                    {
                        if (!first) builder.Append(',');
                        first = false;
                        NewLine(builder, indent + 1);
                        WriteString(builder, Convert.ToString(entry.Key, CultureInfo.InvariantCulture));
                        builder.Append(indent >= 0 ? ": " : ":");
                        Write(builder, entry.Value, indent >= 0 ? indent + 1 : -1);
                    }
                    if (!first) NewLine(builder, indent);
                    builder.Append('}');
                    return;
                }

                IEnumerable sequence = value as IEnumerable;
                if (sequence != null)
                {
                    builder.Append('[');
                    bool first = true;
                    foreach (object item in sequence)
                    {
                        if (!first) builder.Append(',');
                        first = false;
                        NewLine(builder, indent + 1);
                        Write(builder, item, indent >= 0 ? indent + 1 : -1);
                    }
                    if (!first) NewLine(builder, indent);
                    builder.Append(']');
                    return;
                }

                // Anything the converter did not recognise: say what it was.
                WriteString(builder, Convert.ToString(value, CultureInfo.InvariantCulture));
            }

            private static void NewLine(StringBuilder builder, int indent)
            {
                if (indent < 0) return;
                builder.Append('\n');
                builder.Append(' ', indent * 2);
            }

            private static void WriteString(StringBuilder builder, string text)
            {
                builder.Append('"');
                foreach (char character in text)
                {
                    switch (character)
                    {
                        case '"': builder.Append("\\\""); break;
                        case '\\': builder.Append("\\\\"); break;
                        case '\b': builder.Append("\\b"); break;
                        case '\f': builder.Append("\\f"); break;
                        case '\n': builder.Append("\\n"); break;
                        case '\r': builder.Append("\\r"); break;
                        case '\t': builder.Append("\\t"); break;
                        default:
                            if (character < ' ')
                            {
                                builder.Append("\\u").Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                            }
                            else
                            {
                                builder.Append(character);
                            }
                            break;
                    }
                }
                builder.Append('"');
            }
        }

        #endregion
    }
}

#endif
