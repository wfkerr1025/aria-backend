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
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEditorInternal;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.SceneManagement;
#if ENABLE_INPUT_SYSTEM
using UnityEngine.InputSystem;
using UnityEngine.InputSystem.LowLevel;
#endif
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
        public const string Version = "1.4.0";

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
            EditorApplication.update += Pump;
            EditorApplication.playModeStateChanged += OnPlayModeChanged;

            // The console, kept where a caller outside the editor can read it,
            // and carried across the reload that entering play mode causes.
            RestoreLog();
            _mainThread = System.Threading.Thread.CurrentThread.ManagedThreadId;
            Application.logMessageReceivedThreaded += OnLogMessage;
            AssemblyReloadEvents.beforeAssemblyReload += SaveLog;
            UnityEditor.Compilation.CompilationPipeline.compilationStarted += OnCompilationStarted;
            UnityEditor.Compilation.CompilationPipeline.assemblyCompilationFinished += OnAssemblyCompiled;
            UnityEditor.Compilation.CompilationPipeline.compilationFinished += OnCompilationFinished;

#if ENABLE_INPUT_SYSTEM
            // Devices a session added and a reload orphaned before it could
            // take them away. Left a moment, because the Input System may not
            // be up yet this early in a reload.
            if (!EditorApplication.isPlayingOrWillChangePlaymode) EditorApplication.delayCall += RemoveAriaDevices;
#endif

            // A test folder named for a play session that never began -- the
            // entry failed on a compile error, say -- must not still be named
            // when a person next presses Play. The one exception is a request
            // on its way in: entering play mode can recompile first, and that
            // reload lands here before the session it belongs to has started.
            if (!EditorApplication.isPlayingOrWillChangePlaymode && !StartPending()) EndTestSave();

            Debug.Log("[ARIA] Editor bridge " + Version + " loaded. Commands: " + CommandsPath);
        }

        #region Triggers

        /// <summary>
        /// What a playing editor will run: nothing that can change a scene.
        ///
        /// Play mode throws away every edit made during it, so a SetField sent
        /// while the game runs would report success for work about to vanish.
        /// That was the reason for refusing everything while playing -- and it
        /// also meant a bridge that had just been told to START the game could
        /// no longer be pinged, photographed, or told to stop. These can be:
        /// they read, they draw, or they end play mode.
        ///
        /// Mirrored as PLAY_SAFE_COMMANDS in aria/unity_editor_bridge.py, and a
        /// test holds the two lists together.
        /// </summary>
        private static readonly HashSet<string> PlaySafeCommands =
            new HashSet<string>(StringComparer.Ordinal)
        {
            "Ping", "Screenshot", "GetField", "GetHierarchy", "SetPlayMode",
            "SendInput", "ReadScreen", "GetLog",
        };

        /// <summary>The commands file last left waiting for play to stop, by write time.</summary>
        private static DateTime _deferredStamp = DateTime.MinValue;

        private static void Poll()
        {
            if (!Watching) return;

            double now = EditorApplication.timeSinceStartup;
            if (now - _lastPoll < PollIntervalSeconds) return;
            _lastPoll = now;

            // Between modes the domain is about to be torn down, and anything
            // started now would be run by an editor that is not there to finish.
            bool playing = EditorApplication.isPlaying;
            if (EditorApplication.isPlayingOrWillChangePlaymode != playing) return;

            if (EditorApplication.isCompiling || EditorApplication.isUpdating) return;
            if (!File.Exists(CommandsPath)) return;

            if (playing)
            {
                // A batch that edits a scene is LEFT, not refused: it runs the
                // moment the game stops, which is what whoever sent it meant.
                // Read once per version of the file rather than once per poll.
                DateTime stamp = File.GetLastWriteTimeUtc(CommandsPath);
                if (stamp == _deferredStamp) return;

                string text;
                try
                {
                    text = File.ReadAllText(CommandsPath);
                }
                catch (IOException)
                {
                    return;
                }

                string blocked;
                if (!PlaySafe(text, out blocked))
                {
                    _deferredStamp = stamp;
                    return;
                }
            }

            RunPendingCommands();
        }

        /// <summary>Whether every command in a commands document may run while the game plays.</summary>
        private static bool PlaySafe(string text, out string blocked)
        {
            blocked = null;

            object parsed;
            string parseError;
            if (!Json.TryParse(text, out parsed, out parseError))
            {
                blocked = "an unreadable commands file";
                return false;
            }

            List<object> commands = parsed as List<object>;
            Dictionary<string, object> envelope = parsed as Dictionary<string, object>;

            if (envelope != null)
            {
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
                blocked = "a commands file with no commands";
                return false;
            }

            foreach (object entry in commands)
            {
                Dictionary<string, object> one = entry as Dictionary<string, object>;
                string name = one == null ? null : Str(one, "command");

                if (string.IsNullOrEmpty(name) || !PlaySafeCommands.Contains(name))
                {
                    blocked = string.IsNullOrEmpty(name) ? "an unnamed command" : name;
                    return false;
                }
            }

            return true;
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

            if (EditorApplication.isPlayingOrWillChangePlaymode != EditorApplication.isPlaying)
            {
                ok = false;
                message = "Play mode is changing. Send the commands again once it has settled.";
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

                string blocked;
                if (EditorApplication.isPlaying && !PlaySafe(text, out blocked))
                {
                    // Refused, and the file KEPT: it runs when the game stops.
                    ok = false;
                    message = "Not while the game is playing: " + blocked + " can change the scene, " +
                              "and play mode throws such changes away. It will run when the game stops. " +
                              "While playing, only " + string.Join(", ", PlaySafeCommands) + " run.";
                }
                else
                {
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
                case "RefreshAssets": return RefreshAssets(args, scope);
                case "Screenshot": return Screenshot(args, scope);
                case "SetPlayMode": return SetPlayMode(args, scope);
                case "SendInput": return SendInput(args, scope);
                case "ReadScreen": return ReadScreen(args, scope);
                case "GetLog": return GetLog(args, scope);
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
            data["focused"] = InternalEditorUtility.isApplicationActive;
            Scene active = SceneManager.GetActiveScene();
            data["activeScene"] = active.path ?? "";
            data["activeSceneName"] = active.name ?? "";

            // Whether a test is running, and on what. A caller that cannot see
            // the editor has no other way to know the game in front of it is
            // playing against the test folder and not the player's save.
            data["driving"] = Driving;
            data["testSave"] = ActiveTestSave.Replace('\\', '/');
            data["frame"] = EditorApplication.isPlaying ? Time.frameCount : 0;
            data["runInBackground"] = Application.runInBackground;
            data["playerSettingsRunInBackground"] = PlayerSettings.runInBackground;
            data["inputPending"] = PendingInput;
            data["logNext"] = LogNext;
            data["sessionErrors"] = ErrorsSince(SessionLogStart());
            data["burstPending"] = PendingShots;
            data["lastBurst"] = _lastBurst;
#if ENABLE_INPUT_SYSTEM
            // Whether a test session's input routing and devices are in place
            // -- and, once it has ended, that they are gone again.
            data["inputRoutedToGame"] = InputSystem.settings != null &&
                                        InputSystem.settings.name == DrivenInputSettingsName;
            int ariaDevices = 0;
            foreach (InputDevice device in InputSystem.devices)
            {
                if (device.name != null && device.name.StartsWith(AriaDevicePrefix, StringComparison.Ordinal)) ariaDevices++;
            }
            data["ariaDevices"] = ariaDevices;
#endif
            data["lastPlay"] = ReadReport(LastPlayPath);
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

        #region Commands: looking at the result

        /// <summary>
        /// Import what has changed on disk, and say what Unity made of it.
        ///
        /// The reporting half is the point. A tool that writes a .png and a
        /// hand-rolled .meta beside it has no idea whether the pair imported
        /// as a Sprite, as a plain Texture, or at all -- the file is there
        /// either way and the mistake only shows up as an empty square in a
        /// running game. Asking the AssetDatabase turns that into an answer.
        /// </summary>
        private static CommandResult RefreshAssets(Dictionary<string, object> args, Scope scope)
        {
            if (scope.InPrefab) return Fail("RefreshAssets is not allowed inside ModifyPrefab.");

            ImportAssetOptions options = ImportAssetOptions.Default;
            if (Bool(args, "force", false)) options |= ImportAssetOptions.ForceUpdate;
            if (Bool(args, "recursive", false)) options |= ImportAssetOptions.ImportRecursive;

            string path = Str(args, "path", "assetPath", "asset");

            Dictionary<string, object> data = new Dictionary<string, object>();

            if (string.IsNullOrEmpty(path))
            {
                AssetDatabase.Refresh(options);
                data["scope"] = "all";
                return Ok("Refreshed the asset database.", data);
            }

            string error;
            if (!SafeAssetPath(path, null, out error)) return Fail(error);

            string full = Path.Combine(ProjectRoot, path);
            if (!File.Exists(full) && !Directory.Exists(full))
            {
                return Fail("Nothing at " + path + " to import.");
            }

            AssetDatabase.ImportAsset(path, options);

            data["scope"] = "path";
            data["path"] = path;
            data["guid"] = AssetDatabase.AssetPathToGUID(path);

            Describe(path, data);

            return Ok("Imported " + path + ".", data);
        }

        /// <summary>What one asset turned into, in the terms its caller cares about.</summary>
        private static void Describe(string path, Dictionary<string, object> data)
        {
            if (AssetDatabase.IsValidFolder(path))
            {
                data["type"] = "Folder";
                return;
            }

            Object main = AssetDatabase.LoadMainAssetAtPath(path);
            data["type"] = main == null ? "" : main.GetType().Name;
            data["loaded"] = main != null;

            AssetImporter importer = AssetImporter.GetAtPath(path);
            data["importer"] = importer == null ? "" : importer.GetType().Name;

            // Sub-assets are how a Sprite arrives: a texture importer set to
            // Sprite mode leaves a Texture2D as the main asset and hangs the
            // Sprite off it, so "is it a sprite" cannot be answered by the
            // main asset's type alone. This is the question I have been
            // answering by trusting a copied template.
            Object[] all = AssetDatabase.LoadAllAssetsAtPath(path);
            List<object> sprites = new List<object>();
            for (int index = 0; index < all.Length; index++)
            {
                Sprite sprite = all[index] as Sprite;
                if (sprite == null) continue;

                Dictionary<string, object> one = new Dictionary<string, object>();
                one["name"] = sprite.name;
                one["rect"] = sprite.rect.width + "x" + sprite.rect.height;
                one["pixelsPerUnit"] = sprite.pixelsPerUnit;
                sprites.Add(one);
            }
            data["sprites"] = sprites;

            TextureImporter texture = importer as TextureImporter;
            if (texture == null) return;

            data["textureType"] = texture.textureType.ToString();
            data["spriteMode"] = texture.spriteImportMode.ToString();
            data["spritePixelsPerUnit"] = texture.spritePixelsPerUnit;
            data["alphaIsTransparency"] = texture.alphaIsTransparency;
            data["readable"] = texture.isReadable;
            data["maxTextureSize"] = texture.maxTextureSize;
        }

        /// <summary>
        /// Render a camera to a PNG and say where it landed.
        ///
        /// Through a RenderTexture rather than ScreenCapture, because
        /// ScreenCapture writes at the end of a frame and this has to return
        /// an answer inside one command -- a path handed back before the file
        /// exists is worse than no command at all.
        ///
        /// Writes under ARIA/ by default rather than under Assets/, since a
        /// screenshot is evidence, not content, and one dropped in Assets/ is
        /// a new asset to import, re-import and eventually explain.
        /// </summary>
        private static CommandResult Screenshot(Dictionary<string, object> args, Scope scope)
        {
            if (scope.InPrefab) return Fail("Screenshot is not allowed inside ModifyPrefab.");

            int count = (int)Num(args, 1, "count");
            if (count > 1) return StartBurst(args, count);

            int width = (int)Num(args, 1280, "width");
            int height = (int)Num(args, 720, "height");

            if (width < 16 || height < 16) return Fail("A screenshot must be at least 16x16.");
            if (width > 8192 || height > 8192) return Fail("A screenshot must be at most 8192x8192.");

            string view = Str(args, "view", "camera", "source");
            bool wantsScene = !string.IsNullOrEmpty(view) &&
                              Normalise(view) == Normalise("scene");

            string path = Str(args, "path", "file", "output");
            if (string.IsNullOrEmpty(path))
            {
                path = "ARIA/shots/shot_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".png";
            }

            path = path.Replace('\\', '/').Trim();
            if (path.Contains("..") || Path.IsPathRooted(path))
            {
                return Fail("A screenshot path must be relative to the project and free of '..'.");
            }
            if (!path.EndsWith(".png", StringComparison.OrdinalIgnoreCase)) path += ".png";

            // After the path is known to be good, so a refused path cannot
            // leave a borrowed camera behind.
            GameObject borrowed;
            string cameraError;
            Camera camera = FindCaptureCamera(wantsScene, out borrowed, out cameraError);
            if (camera == null) return Fail(cameraError);

            try
            {
                int canvases;
                Texture2D flat = CaptureFrame(camera, width, height, wantsScene, out canvases);
                byte[] png = flat.EncodeToPNG();
                Object.DestroyImmediate(flat);

                string full = Path.Combine(ProjectRoot, path);
                string folder = Path.GetDirectoryName(full);
                if (!string.IsNullOrEmpty(folder)) Directory.CreateDirectory(folder);

                File.WriteAllBytes(full, png);

                Dictionary<string, object> data = new Dictionary<string, object>();
                data["path"] = path;
                data["absolutePath"] = full.Replace('\\', '/');
                data["width"] = width;
                data["height"] = height;
                data["bytes"] = png.Length;
                data["camera"] = camera.name;
                data["view"] = wantsScene ? "scene" : "game";
                data["canvases"] = canvases;
                data["isPlaying"] = EditorApplication.isPlaying;

                // Said out loud, because an empty-looking screenshot of this
                // project is nearly always this and not a broken camera.
                if (!EditorApplication.isPlaying && canvases == 0)
                {
                    data["note"] = "Nothing is playing and no canvas was found. A game that builds "
                                 + "its interface at runtime has none to photograph until it runs: "
                                 + "SetPlayMode first.";
                }

                if (path.StartsWith("Assets/", StringComparison.Ordinal)) AssetDatabase.ImportAsset(path);

                return Ok("Wrote " + path + ".", data);
            }
            catch (Exception error)
            {
                return Fail("Screenshot failed: " + error.Message);
            }
            finally
            {
                if (borrowed != null) Object.DestroyImmediate(borrowed);
            }
        }

        /// <summary>The camera a screenshot is taken through, or a borrowed one when the scene has none.</summary>
        private static Camera FindCaptureCamera(bool wantsScene, out GameObject borrowed, out string error)
        {
            borrowed = null;
            error = null;

            if (wantsScene)
            {
                SceneView sceneView = SceneView.lastActiveSceneView;
                if (sceneView == null)
                {
                    error = "There is no Scene view open to capture.";
                    return null;
                }
                return sceneView.camera;
            }

            Camera found = Camera.main;
            if (found == null)
            {
                Camera[] all = FindAll<Camera>();
                for (int index = 0; index < all.Length; index++)
                {
                    if (!all[index].enabled || !all[index].gameObject.activeInHierarchy) continue;
                    found = all[index];
                    break;
                }
            }

            // No camera anywhere is still worth a picture: the UI is the usual
            // reason for asking, and it does not need one of its own.
            if (found == null)
            {
                borrowed = new GameObject("ARIA Screenshot Camera");
                borrowed.hideFlags = HideFlags.HideAndDontSave;
                found = borrowed.AddComponent<Camera>();
                found.clearFlags = CameraClearFlags.SolidColor;
                found.backgroundColor = new Color(0.08f, 0.08f, 0.10f, 1f);
            }

            return found;
        }

        /// <summary>
        /// One frame of a camera, as a texture the caller owns and destroys.
        /// Shared by a single screenshot and every shot of a burst.
        /// </summary>
        private static Texture2D CaptureFrame(Camera camera, int width, int height, bool wantsScene, out int canvases)
        {
            RenderTexture picture = null;
            RenderTexture wasActive = RenderTexture.active;
            RenderTexture wasTarget = camera.targetTexture;

            List<Canvas> moved = new List<Canvas>();
            List<RenderMode> wereModes = new List<RenderMode>();
            List<Camera> wereCameras = new List<Camera>();
            List<float> wereDistances = new List<float>();

            try
            {
                picture = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32);
                picture.antiAliasing = 1;
                picture.Create();

                // Screen Space - Overlay draws straight to the display and
                // appears in no camera's render, so a screenshot of a game
                // whose entire interface is built at runtime comes back as an
                // empty room. Borrowing each canvas onto the capture camera
                // for one render is what puts the interface in the picture;
                // the finally below hands them all back.
                if (!wantsScene) BorrowCanvases(camera, moved, wereModes, wereCameras, wereDistances);
                canvases = moved.Count;

                Render(camera, picture);

                Texture2D flat = new Texture2D(width, height, TextureFormat.RGB24, false);
                RenderTexture.active = picture;
                flat.ReadPixels(new Rect(0, 0, width, height), 0, 0);
                flat.Apply();
                return flat;
            }
            finally
            {
                for (int index = 0; index < moved.Count; index++)
                {
                    if (moved[index] == null) continue;
                    moved[index].renderMode = wereModes[index];
                    moved[index].worldCamera = wereCameras[index];
                    moved[index].planeDistance = wereDistances[index];
                }

                RenderTexture.active = wasActive;
                camera.targetTexture = wasTarget;

                if (picture != null)
                {
                    picture.Release();
                    Object.DestroyImmediate(picture);
                }
            }
        }

        /// <summary>A run of screenshots taken while the game plays.</summary>
        private sealed class Burst
        {
            public string BasePath;
            public int Count;
            public int Every;
            public double EverySeconds;
            public int Width;
            public int Height;
            public bool WantsScene;
            public bool Sheet;
            public int Columns;
            public bool Started;
            public int LastFrame;
            public double LastTime;
            public double StartTime;
            public string Error;
            public readonly List<Texture2D> Frames = new List<Texture2D>();
            public readonly List<object> Shots = new List<object>();
        }

        private const int BurstMost = 64;
        private static Burst _burst;
        private static string _lastBurst = "";

        /// <summary>Shots a burst still has to take; 0 when none is running.</summary>
        private static int PendingShots
        {
            get { return _burst == null ? 0 : _burst.Count - _burst.Shots.Count; }
        }

        /// <summary>
        /// Screenshot with a count: a run of shots while the game plays.
        ///
        /// A lot of what a game does moves -- a charm's sway, capsules
        /// tumbling, a knob turning, a character's wave -- and one frame of it
        /// says nothing about whether it moves right. Shots are taken every
        /// everySeconds (0.1 unless every, in frames, is given instead), each
        /// saved as base_00.png, base_01.png..., and laid out on one contact
        /// sheet, base_sheet.png, left to right and top to bottom, so the
        /// motion can be read from one picture. base.json lists every shot
        /// with its frame and time. Returns before the first shot: Ping's
        /// burstPending reaches 0 when the run is done.
        /// </summary>
        private static CommandResult StartBurst(Dictionary<string, object> args, int count)
        {
            if (!EditorApplication.isPlaying)
            {
                return Fail("A burst watches something move, and in edit mode nothing does. SetPlayMode first, " +
                            "or take a single screenshot.");
            }
            if (_burst != null)
            {
                return Fail("A burst is already being taken, " + PendingShots + " shot(s) to go. Wait for " +
                            "Ping's burstPending to reach 0.");
            }
            if (count > BurstMost) return Fail("A burst is at most " + BurstMost + " shots.");

            int width = (int)Num(args, 640, "width");
            int height = (int)Num(args, 360, "height");
            if (width < 16 || height < 16 || width > 4096 || height > 4096)
            {
                return Fail("A burst's shots are 16 to 4096 pixels on a side.");
            }

            // Seconds unless frames are asked for: what a person watches -- a
            // sway, a tumble -- happens in time, and at hundreds of frames a
            // second "every 5 frames" is a blur of nearly identical pictures.
            bool framesAsked = args.ContainsKey("every") || args.ContainsKey("everyFrames");
            int every = Math.Max(1, (int)Num(args, 1, "every", "everyFrames"));
            double everySeconds = Math.Max(0, Num(args, framesAsked ? 0 : 0.1, "everySeconds"));

            string view = Str(args, "view", "camera", "source");
            bool wantsScene = !string.IsNullOrEmpty(view) && Normalise(view) == Normalise("scene");

            string path = Str(args, "path", "file", "output");
            if (string.IsNullOrEmpty(path)) path = "ARIA/shots/burst_" + DateTime.Now.ToString("yyyyMMdd_HHmmss");
            path = path.Replace('\\', '/').Trim();
            if (path.Contains("..") || Path.IsPathRooted(path))
            {
                return Fail("A screenshot path must be relative to the project and free of '..'.");
            }
            if (path.EndsWith(".png", StringComparison.OrdinalIgnoreCase)) path = path.Substring(0, path.Length - 4);

            int columns = (int)Num(args, Math.Ceiling(Math.Sqrt(count)), "columns");
            columns = Math.Max(1, Math.Min(count, columns));
            bool sheet = Bool(args, "sheet", true);

            _burst = new Burst
            {
                BasePath = path,
                Count = count,
                Every = every,
                EverySeconds = everySeconds,
                Width = width,
                Height = height,
                WantsScene = wantsScene,
                Sheet = sheet,
                Columns = columns,
                StartTime = EditorApplication.timeSinceStartup,
            };

            List<object> planned = new List<object>();
            for (int index = 0; index < count; index++)
            {
                planned.Add(path + "_" + index.ToString("00", CultureInfo.InvariantCulture) + ".png");
            }

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["burst"] = true;
            data["count"] = count;
            data["every"] = every;
            data["everySeconds"] = everySeconds;
            data["width"] = width;
            data["height"] = height;
            data["shots"] = planned;
            data["sheet"] = sheet ? path + "_sheet.png" : null;
            data["manifest"] = path + ".json";
            data["frame"] = Time.frameCount;

            string spacing = everySeconds > 0
                ? everySeconds.ToString("0.###", CultureInfo.InvariantCulture) + " s"
                : every + " frame(s)";
            return Ok("Taking " + count + " shots, one every " + spacing + ". Ping's burstPending reaches 0 " +
                      "when they are done, and " + path + ".json lists them.", data);
        }

        /// <summary>Take the burst's next shot when its time has come. Called every editor update.</summary>
        private static void PumpBurst()
        {
            if (_burst == null) return;

            if (!EditorApplication.isPlaying)
            {
                _burst.Error = "play mode ended with " + PendingShots + " shot(s) still to take";
                FinishBurst();
                return;
            }

            int frame = Time.frameCount;
            double now = EditorApplication.timeSinceStartup;
            if (_burst.Started && (frame < _burst.LastFrame + _burst.Every || now < _burst.LastTime + _burst.EverySeconds))
            {
                return;
            }

            _burst.Started = true;
            _burst.LastFrame = frame;
            _burst.LastTime = now;

            GameObject borrowed = null;
            try
            {
                string error;
                Camera camera = FindCaptureCamera(_burst.WantsScene, out borrowed, out error);
                if (camera == null)
                {
                    _burst.Error = error;
                    FinishBurst();
                    return;
                }

                int canvases;
                Texture2D flat = CaptureFrame(camera, _burst.Width, _burst.Height, _burst.WantsScene, out canvases);

                string path = _burst.BasePath + "_" + _burst.Shots.Count.ToString("00", CultureInfo.InvariantCulture) + ".png";
                string full = Path.Combine(ProjectRoot, path);
                Directory.CreateDirectory(Path.GetDirectoryName(full));
                File.WriteAllBytes(full, flat.EncodeToPNG());

                if (_burst.Sheet) _burst.Frames.Add(flat);
                else Object.DestroyImmediate(flat);

                Dictionary<string, object> shot = new Dictionary<string, object>();
                shot["path"] = path;
                shot["frame"] = frame;
                shot["seconds"] = Math.Round(now - _burst.StartTime, 3);
                shot["canvases"] = canvases;
                _burst.Shots.Add(shot);
            }
            catch (Exception failure)
            {
                _burst.Error = "shot " + (_burst.Shots.Count + 1) + " failed: " + failure.Message;
                FinishBurst();
                return;
            }
            finally
            {
                if (borrowed != null) Object.DestroyImmediate(borrowed);
            }

            if (_burst.Shots.Count >= _burst.Count) FinishBurst();
        }

        /// <summary>Lay out the contact sheet, write the manifest, and let the textures go.</summary>
        private static void FinishBurst()
        {
            Burst done = _burst;
            _burst = null;
            if (done == null) return;

            Dictionary<string, object> manifest = new Dictionary<string, object>();
            manifest["shots"] = done.Shots;
            manifest["count"] = done.Count;
            manifest["taken"] = done.Shots.Count;
            manifest["width"] = done.Width;
            manifest["height"] = done.Height;
            manifest["every"] = done.Every;
            manifest["everySeconds"] = done.EverySeconds;
            if (done.Error != null) manifest["error"] = done.Error;

            try
            {
                if (done.Sheet && done.Frames.Count > 0)
                {
                    string sheetError;
                    byte[] png = ComposeSheet(done.Frames, done.Columns, out sheetError);
                    if (png != null)
                    {
                        string sheetPath = done.BasePath + "_sheet.png";
                        File.WriteAllBytes(Path.Combine(ProjectRoot, sheetPath), png);
                        manifest["sheet"] = sheetPath;
                        manifest["columns"] = done.Columns;
                    }
                    else
                    {
                        manifest["sheetError"] = sheetError;
                    }
                }
            }
            catch (Exception failure)
            {
                manifest["sheetError"] = failure.Message;
            }
            finally
            {
                foreach (Texture2D frame in done.Frames)
                {
                    if (frame != null) Object.DestroyImmediate(frame);
                }
            }

            string manifestPath = done.BasePath + ".json";
            WriteReport(Path.Combine(ProjectRoot, manifestPath), manifest);
            _lastBurst = manifestPath;
        }

        /// <summary>
        /// Every shot on one picture, in reading order: left to right, then
        /// down. A texture's rows count from the bottom, so the first row of
        /// shots is laid at the top.
        /// </summary>
        private static byte[] ComposeSheet(List<Texture2D> frames, int columns, out string error)
        {
            error = null;
            const int gap = 4;

            int width = frames[0].width;
            int height = frames[0].height;
            int rows = (frames.Count + columns - 1) / columns;
            int sheetWidth = columns * width + (columns + 1) * gap;
            int sheetHeight = rows * height + (rows + 1) * gap;

            if (sheetWidth > 8192 || sheetHeight > 8192)
            {
                error = "the sheet would be " + sheetWidth + "x" + sheetHeight + ", over 8192 a side; " +
                        "take smaller or fewer shots";
                return null;
            }

            Texture2D sheet = new Texture2D(sheetWidth, sheetHeight, TextureFormat.RGB24, false);
            try
            {
                Color32[] ground = new Color32[sheetWidth * sheetHeight];
                Color32 dark = new Color32(24, 24, 28, 255);
                for (int index = 0; index < ground.Length; index++) ground[index] = dark;
                sheet.SetPixels32(ground);

                for (int index = 0; index < frames.Count; index++)
                {
                    int column = index % columns;
                    int row = index / columns;
                    int x = gap + column * (width + gap);
                    int y = sheetHeight - (row + 1) * (height + gap);
                    sheet.SetPixels32(x, y, width, height, frames[index].GetPixels32());
                }

                sheet.Apply();
                return sheet.EncodeToPNG();
            }
            finally
            {
                Object.DestroyImmediate(sheet);
            }
        }

        /// <summary>Put every overlay canvas in front of one camera, remembering where it was.</summary>
        private static void BorrowCanvases(Camera camera, List<Canvas> moved, List<RenderMode> modes,
                                           List<Camera> cameras, List<float> distances)
        {
            Canvas[] all = FindAll<Canvas>();

            for (int index = 0; index < all.Length; index++)
            {
                Canvas canvas = all[index];
                if (canvas == null || !canvas.isActiveAndEnabled) continue;
                if (canvas.renderMode != RenderMode.ScreenSpaceOverlay) continue;

                // Only the roots. A nested canvas inherits its parent's mode
                // and setting it here would detach it from the one above.
                if (canvas.transform.parent != null &&
                    canvas.transform.parent.GetComponentInParent<Canvas>() != null) continue;

                moved.Add(canvas);
                modes.Add(canvas.renderMode);
                cameras.Add(canvas.worldCamera);
                distances.Add(canvas.planeDistance);

                canvas.renderMode = RenderMode.ScreenSpaceCamera;
                canvas.worldCamera = camera;
                canvas.planeDistance = Mathf.Max(camera.nearClipPlane + 0.01f, 0.5f);
            }
        }

        /// <summary>Draw one camera into one texture, whichever pipeline is installed.</summary>
        private static void Render(Camera camera, RenderTexture texture)
        {
            // Camera.Render is the built-in pipeline's call and is unsupported
            // under URP and HDRP, where the render request is the way in. Ask
            // first: the answer depends on the project this was installed in,
            // not on the version it was written against.
#if UNITY_2023_1_OR_NEWER
            // Render requests arrived in 2023.1. Before that there is only
            // Camera.Render, and a URP project on an older editor gets the
            // warning Unity gives for it instead of a picture.
            RenderPipeline.StandardRequest request = new RenderPipeline.StandardRequest();
            request.destination = texture;

            if (RenderPipelineManager.currentPipeline != null &&
                RenderPipeline.SupportsRenderRequest(camera, request))
            {
                camera.SubmitRenderRequest(request);
                return;
            }
#endif

            camera.targetTexture = texture;
            camera.Render();
        }

        /// <summary>Every live object of a type, through whichever API this editor has.</summary>
        private static T[] FindAll<T>() where T : Object
        {
#if UNITY_2022_2_OR_NEWER
            return Object.FindObjectsByType<T>(FindObjectsSortMode.None);
#else
            return Object.FindObjectsOfType<T>();
#endif
        }

        /// <summary>How many play-mode snapshots are kept under ARIA/snapshots.</summary>
        private const int SnapshotsKept = 10;

        /// <summary>A persistentDataPath bigger than this is refused rather than copied.</summary>
        private const long SnapshotByteLimit = 256L * 1024 * 1024;

        /// <summary>
        /// Copy everything the game keeps in persistentDataPath, before play
        /// mode gets the chance to write over it.
        ///
        /// A bug in the game's save code, or a test driving the game somewhere
        /// its author never took it, can replace hours of play in one frame. A
        /// folder copied a moment earlier is the difference between an apology
        /// and a restore. A file that cannot be read is skipped and named in
        /// the result rather than sinking the copy; the saves are what matter.
        /// </summary>
        private static Dictionary<string, object> SnapshotPersistentData(out string error)
        {
            error = null;

            string source = Application.persistentDataPath;
            Dictionary<string, object> data = new Dictionary<string, object>();
            data["source"] = (source ?? "").Replace('\\', '/');

            if (string.IsNullOrEmpty(source) || !Directory.Exists(source))
            {
                // Nothing saved yet is not a failure: there is nothing to lose.
                data["path"] = "";
                data["files"] = 0;
                data["bytes"] = 0L;
                return data;
            }

            try
            {
                string[] files = Directory.GetFiles(source, "*", SearchOption.AllDirectories);

                long total = 0;
                foreach (string file in files) total += new FileInfo(file).Length;

                if (total > SnapshotByteLimit)
                {
                    error = "persistentDataPath holds " + (total / (1024 * 1024)) + " MB, over the " +
                            (SnapshotByteLimit / (1024 * 1024)) + " MB a snapshot will copy";
                    return null;
                }

                string root = Path.Combine(BridgeFolder, "snapshots");
                string target = Path.Combine(root,
                    DateTime.Now.ToString("yyyyMMdd_HHmmss_fff", CultureInfo.InvariantCulture));
                Directory.CreateDirectory(target);

                int copied = 0;
                List<object> skipped = new List<object>();

                foreach (string file in files)
                {
                    string relative = file.Substring(source.Length).TrimStart('\\', '/');
                    string destination = Path.Combine(target, relative);

                    try
                    {
                        Directory.CreateDirectory(Path.GetDirectoryName(destination));
                        File.Copy(file, destination, false);
                        copied++;
                    }
                    catch (Exception failure)
                    {
                        skipped.Add(relative + ": " + failure.Message);
                    }
                }

                PruneSnapshots(root);

                data["path"] = target.Replace('\\', '/');
                data["files"] = copied;
                data["bytes"] = total;
                data["skipped"] = skipped;
                return data;
            }
            catch (Exception failure)
            {
                error = failure.Message;
                return null;
            }
        }

        /// <summary>Keep the newest snapshots; the folder names sort by time.</summary>
        private static void PruneSnapshots(string root)
        {
            string[] kept = Directory.GetDirectories(root);
            Array.Sort(kept, StringComparer.Ordinal);

            for (int index = 0; index < kept.Length - SnapshotsKept; index++)
            {
                try
                {
                    Directory.Delete(kept[index], true);
                }
                catch (Exception failure)
                {
                    Debug.LogWarning("[ARIA] Could not prune snapshot " + kept[index] + ": " + failure.Message);
                }
            }
        }

        #region Play sessions

        /// <summary>
        /// The environment variable a game reads to find the bridge's test
        /// folder. Set before a bridge-started play session, cleared when it
        /// ends; a game that honours it keeps every file it writes there.
        /// </summary>
        public const string TestSaveVariable = "ARIA_TEST_SAVE_DIR";

        private const string TestSaveFolderName = "testsave";
        private const string LastPlayFileName = "last_play.json";

        // SessionState outlives the domain reload that entering play mode
        // causes and dies with the editor, which is exactly a session's life.
        private const string PendingStartKey = "ARIA.Bridge.PendingStart";
        private const string DrivingKey = "ARIA.Bridge.Driving";
        private const string TestSaveKey = "ARIA.Bridge.TestSave";
        private const string ManifestKey = "ARIA.Bridge.RealSaveManifest";
        private const string StartedAtKey = "ARIA.Bridge.StartedAt";

        /// <summary>How long after SetPlayMode a play session can still be claimed as the bridge's.</summary>
        private const double PendingStartSeconds = 15.0;

        /// <summary>Where a bridge-started game keeps its files, beside the snapshots.</summary>
        public static string TestSaveFolder
        {
            get { return Path.Combine(BridgeFolder, TestSaveFolderName); }
        }

        /// <summary>What the last bridge-started session left behind: see EndSession.</summary>
        public static string LastPlayPath
        {
            get { return Path.Combine(BridgeFolder, LastPlayFileName); }
        }

        /// <summary>Whether the game now playing was started by the bridge against its test folder.</summary>
        private static bool Driving
        {
            get { return SessionState.GetBool(DrivingKey, false); }
            set { SessionState.SetBool(DrivingKey, value); }
        }

        /// <summary>The test folder the environment names right now, or "".</summary>
        private static string ActiveTestSave
        {
            get { return Environment.GetEnvironmentVariable(TestSaveVariable) ?? ""; }
        }

        private static bool StartPending()
        {
            double asked;
            string pending = SessionState.GetString(PendingStartKey, "");
            return double.TryParse(pending, NumberStyles.Float, CultureInfo.InvariantCulture, out asked) &&
                   EditorApplication.timeSinceStartup - asked < PendingStartSeconds;
        }

        private static void OnPlayModeChanged(PlayModeStateChange change)
        {
            if (change == PlayModeStateChange.ExitingEditMode) ClaimOrDisownStart();
            else if (change == PlayModeStateChange.EnteredPlayMode) BeginDrivenPlay();
            else if (change == PlayModeStateChange.ExitingPlayMode) EndDrivenPlay();
            else if (change == PlayModeStateChange.EnteredEditMode) EndSession();
        }

        /// <summary>Whether a driven session keeps running while Unity is not the focused application.</summary>
        private const string KeepRunningKey = "ARIA.Bridge.KeepRunning";

        /// <summary>What Application.runInBackground was before a driven session changed it.</summary>
        private static bool _wasRunningInBackground;
        private static bool _changedRunInBackground;

        /// <summary>
        /// Keep a bridge-started game advancing while Unity is in the background.
        ///
        /// With Run In Background off, an unfocused editor plays no frames.
        /// Measured on 6000.3 with the window minimized mid-play: the frame
        /// count stood at 456 for three seconds with the flag off, and went
        /// from 1823 to 3422 with it on. Start never comes, coroutines stall,
        /// and a test waits on a game that is not running -- and the bridge is
        /// used exactly while the person is somewhere else. So for its own
        /// sessions, and only for their length, the game runs regardless.
        ///
        /// In the editor the runtime flag IS Player Settings' Run In
        /// Background: both read true for the session. The old value is
        /// handed back as play ends, and nothing is written to
        /// ProjectSettings.asset unless something saves the project while the
        /// session runs -- in which case the next save puts it right.
        ///
        /// Entering play mode brings the editor window to the front, so an
        /// unfocused START is not the case this is for; a person clicking
        /// away from a running test is.
        /// </summary>
        private static void BeginDrivenPlay()
        {
            if (!Driving) return;

            if (SessionState.GetBool(KeepRunningKey, true))
            {
                _wasRunningInBackground = Application.runInBackground;
                _changedRunInBackground = true;
                Application.runInBackground = true;
            }

#if ENABLE_INPUT_SYSTEM
            RouteInputToGame();
#endif
        }

        /// <summary>Hand back everything BeginDrivenPlay changed.</summary>
        private static void EndDrivenPlay()
        {
#if ENABLE_INPUT_SYSTEM
            RestoreInput();
#endif
            if (!_changedRunInBackground) return;

            Application.runInBackground = _wasRunningInBackground;
            _changedRunInBackground = false;
        }

        /// <summary>
        /// Decide, as play begins, whether this session is the bridge's.
        ///
        /// Only a SetPlayMode from moments ago claims it. Anything else is a
        /// person pressing Play, and whatever an earlier request left behind
        /// must not quietly turn their game into a test that keeps nothing.
        /// The variable is set again on a claim rather than trusted to have
        /// survived: a recompile between the request and the start is a
        /// domain reload, and the one place worth being sure is here.
        /// </summary>
        private static void ClaimOrDisownStart()
        {
            bool claimed = StartPending();
            SessionState.EraseString(PendingStartKey);

            string folder = SessionState.GetString(TestSaveKey, "");

            if (claimed && Driving && !string.IsNullOrEmpty(folder))
            {
                Environment.SetEnvironmentVariable(TestSaveVariable, folder);
                return;
            }

            if (!claimed) EndTestSave();
        }

        /// <summary>Forget the test folder: the variable, and the session's claim to it.</summary>
        private static void EndTestSave()
        {
            Environment.SetEnvironmentVariable(TestSaveVariable, null);
            SessionState.EraseString(TestSaveKey);
            Driving = false;
        }

        /// <summary>
        /// Close a session the bridge started, and say whether the real save survived it.
        ///
        /// The test folder only protects a game that reads the variable. One
        /// that does not writes where it always does, and the only way to
        /// know is to look: every file under persistentDataPath is compared
        /// with the list taken as play began. A difference is logged as an
        /// error and named in ARIA/last_play.json, with the snapshot taken at
        /// the start as the way back.
        /// </summary>
        private static void EndSession()
        {
            bool wasDriving = Driving;
            string folder = SessionState.GetString(TestSaveKey, "");
            string manifest = SessionState.GetString(ManifestKey, "");
            string startedAt = SessionState.GetString(StartedAtKey, "");

            EndTestSave();
            SessionState.EraseString(ManifestKey);
            SessionState.EraseString(StartedAtKey);

#if ENABLE_INPUT_SYSTEM
            RecoverInputSettings();
#endif

            if (!wasDriving) return;

            Dictionary<string, object> report = new Dictionary<string, object>();
            report["startedAt"] = startedAt;
            report["endedAt"] = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);
            report["testSave"] = folder.Replace('\\', '/');
            report["realSave"] = (Application.persistentDataPath ?? "").Replace('\\', '/');

            List<object> touched = CompareRealSave(manifest);
            report["realSaveTouched"] = touched;
            report["realSaveSafe"] = touched.Count == 0;

            // What went wrong while it played, so the first question after a
            // session -- did anything throw? -- is answered without asking.
            long logStart = SessionLogStart();
            report["errors"] = ErrorsSince(logStart);
            report["firstErrors"] = FirstErrorsSince(logStart, 5);
            report["logFrom"] = logStart;

            if (touched.Count > 0)
            {
                Debug.LogError("[ARIA] The game changed its REAL save folder during a test session (" +
                               string.Join(", ", touched.ConvertAll<string>(
                                   item => Convert.ToString(item, CultureInfo.InvariantCulture)).ToArray()) +
                               "). It does not read " + TestSaveVariable + " for every file it writes. " +
                               "The copy taken as play began is in ARIA/snapshots.");
            }

            WriteReport(LastPlayPath, report);
        }

        private static void WriteReport(string path, Dictionary<string, object> report)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                File.WriteAllText(path, Json.Serialize(report, true), new UTF8Encoding(false));
            }
            catch (Exception failure)
            {
                Debug.LogWarning("[ARIA] Could not write " + path + ": " + failure.Message);
            }
        }

        /// <summary>The last session's report, or null when there has not been one.</summary>
        private static object ReadReport(string path)
        {
            try
            {
                if (!File.Exists(path)) return null;

                object parsed;
                string error;
                return Json.TryParse(File.ReadAllText(path), out parsed, out error) ? parsed : null;
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary>
        /// Make the test folder ready for a session.
        ///
        /// keep  -- as the last session left it (a new folder is a new game)
        /// fresh -- emptied, so the game starts from nothing
        /// real  -- emptied, then filled with a copy of the real save, so a
        ///          test starts where the player is without being able to
        ///          write back to where the player is
        /// </summary>
        private static Dictionary<string, object> PrepareTestSave(string seed, out string error)
        {
            error = null;
            string how = Normalise(string.IsNullOrEmpty(seed) ? "keep" : seed);

            if (how != "keep" && how != "fresh" && how != "real")
            {
                error = "seed is keep, fresh or real, not '" + seed + "'";
                return null;
            }

            string folder = Path.GetFullPath(TestSaveFolder);

            // Emptying a folder is only ever done to this one. Checked rather
            // than assumed, because the day it is wrong it deletes something.
            if (!folder.StartsWith(Path.GetFullPath(BridgeFolder), StringComparison.OrdinalIgnoreCase))
            {
                error = "the test folder " + folder + " is not inside " + BridgeFolder;
                return null;
            }

            try
            {
                if (how != "keep" && Directory.Exists(folder)) Directory.Delete(folder, true);
                Directory.CreateDirectory(folder);

                int copied = 0;
                if (how == "real") copied = CopyRealSave(folder);

                Dictionary<string, object> data = new Dictionary<string, object>();
                data["path"] = folder.Replace('\\', '/');
                data["seed"] = how;
                data["copied"] = copied;
                data["files"] = Directory.GetFiles(folder, "*", SearchOption.AllDirectories).Length;
                return data;
            }
            catch (Exception failure)
            {
                error = failure.Message;
                return null;
            }
        }

        /// <summary>Copy the game's saved files, not Unity's own caches, into a folder.</summary>
        private static int CopyRealSave(string into)
        {
            string source = Application.persistentDataPath;
            if (string.IsNullOrEmpty(source) || !Directory.Exists(source)) return 0;

            int copied = 0;
            foreach (string file in Directory.GetFiles(source, "*", SearchOption.AllDirectories))
            {
                string relative = Relative(source, file);
                if (EngineOwned(relative)) continue;

                string destination = Path.Combine(into, relative);
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                File.Copy(file, destination, true);
                copied++;
            }

            return copied;
        }

        /// <summary>Every file under persistentDataPath, by size and write time.</summary>
        private static Dictionary<string, object> RealSaveManifest()
        {
            Dictionary<string, object> manifest = new Dictionary<string, object>();

            string root = Application.persistentDataPath;
            if (string.IsNullOrEmpty(root) || !Directory.Exists(root)) return manifest;

            foreach (string file in Directory.GetFiles(root, "*", SearchOption.AllDirectories))
            {
                string relative = Relative(root, file);
                if (EngineOwned(relative)) continue;

                FileInfo info = new FileInfo(file);
                manifest[relative] = info.Length.ToString(CultureInfo.InvariantCulture) + ":" +
                                     info.LastWriteTimeUtc.Ticks.ToString(CultureInfo.InvariantCulture);
            }

            return manifest;
        }

        /// <summary>What changed under persistentDataPath since a manifest was taken, one line per file.</summary>
        private static List<object> CompareRealSave(string before)
        {
            List<object> touched = new List<object>();

            object parsed;
            string error;
            Dictionary<string, object> old = Json.TryParse(before ?? "", out parsed, out error)
                ? parsed as Dictionary<string, object>
                : null;

            // No list to compare with means nothing can be claimed either way,
            // and "safe" must never be said without having looked.
            if (old == null)
            {
                touched.Add("(no record of the real save from the start of the session)");
                return touched;
            }

            Dictionary<string, object> now;
            try
            {
                now = RealSaveManifest();
            }
            catch (Exception failure)
            {
                touched.Add("(could not read the real save folder: " + failure.Message + ")");
                return touched;
            }

            foreach (KeyValuePair<string, object> file in now)
            {
                object was;
                if (!old.TryGetValue(file.Key, out was)) touched.Add(file.Key + " (created)");
                else if (Convert.ToString(was, CultureInfo.InvariantCulture) != Convert.ToString(file.Value, CultureInfo.InvariantCulture)) touched.Add(file.Key + " (changed)");
            }

            foreach (string file in old.Keys)
            {
                if (!now.ContainsKey(file)) touched.Add(file + " (deleted)");
            }

            return touched;
        }

        private static string Relative(string root, string file)
        {
            return file.Substring(root.Length).TrimStart('\\', '/').Replace('\\', '/');
        }

        /// <summary>Unity keeps caches of its own under persistentDataPath/Unity; those are not the game's save.</summary>
        private static bool EngineOwned(string relative)
        {
            return relative.StartsWith("Unity/", StringComparison.OrdinalIgnoreCase);
        }

        #endregion

        /// <summary>
        /// Start or stop the game.
        ///
        /// Returns before it happens, and that is not a shortcoming. Entering
        /// play mode reloads the C# domain, which throws away everything
        /// holding this call -- so the result has to be written first. The
        /// file-based design is what makes that survivable: the next batch is
        /// read by a bridge that came back up on the other side, already
        /// playing -- one that runs only PlaySafeCommands, so that batch is a
        /// Ping, a Screenshot, a read, or this command again to stop.
        ///
        /// Entering is guarded twice, because play mode runs the game's own
        /// save code against the player's real files: it refuses while Unity
        /// is not the focused application (allowUnfocused overrides), and it
        /// copies persistentDataPath to ARIA/snapshots first (skipSnapshot
        /// overrides). Both were learned from a save that did not survive.
        /// </summary>
        private static CommandResult SetPlayMode(Dictionary<string, object> args, Scope scope)
        {
            if (scope.InPrefab) return Fail("SetPlayMode is not allowed inside ModifyPrefab.");
            if (Application.isBatchMode) return Fail("A batch-mode editor cannot enter play mode.");

            bool wanted = Bool(args, "playing", Bool(args, "play", true));
            bool already = EditorApplication.isPlaying;

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["was"] = already;
            data["willBe"] = wanted;
            data["changed"] = already != wanted;

            if (already == wanted)
            {
                return Ok(wanted ? "Already playing." : "Already stopped.", data);
            }

            if (!wanted)
            {
                EditorApplication.ExitPlaymode();
                return Ok("Leaving play mode.", data);
            }

            bool focused = InternalEditorUtility.isApplicationActive;
            data["focused"] = focused;

            bool testSave = Bool(args, "testSave", true);
            bool keepRunning = Bool(args, "keepRunning", true);

            // Unfocused, Unity sends the game OnApplicationFocus(false) as play
            // begins -- before Start, in no promised order -- and with Run In
            // Background off the game will not advance a frame after it.
            // Measured: a game that saves on focus-lost wrote its blank wake-up
            // state over a real save exactly this way. On the test save that
            // write lands in the test folder, and keepRunning supplies the
            // frames, so only a start with either one refused still needs the
            // person's click.
            if (!focused && !(testSave && keepRunning) && !Bool(args, "allowUnfocused", false))
            {
                return Fail("Unity is not the focused application, so play mode was not started. Started " +
                            "now, the game would get a focus-lost event before its Start runs and would " +
                            "not advance a frame. Click into Unity and send again, pass allowUnfocused:true, " +
                            "or leave testSave and keepRunning on.");
            }

            if (!Bool(args, "skipSnapshot", false))
            {
                string snapshotError;
                Dictionary<string, object> snapshot = SnapshotPersistentData(out snapshotError);
                if (snapshot == null)
                {
                    return Fail("Play mode was not started: the game's saved data could not be copied first (" +
                                snapshotError + "). Pass skipSnapshot:true to start without a copy.");
                }
                data["snapshot"] = snapshot;
            }

            // The test save, on unless refused. The snapshot above is the way
            // back after damage; this is what stops the damage, for a game
            // that reads the variable -- and the manifest below is how a game
            // that does not is caught.
            if (testSave)
            {
                string prepareError;
                Dictionary<string, object> prepared = PrepareTestSave(Str(args, "seed"), out prepareError);
                if (prepared == null)
                {
                    return Fail("Play mode was not started: the test save could not be prepared (" + prepareError + ").");
                }

                data["testSave"] = prepared;
                SessionState.SetString(TestSaveKey, Path.GetFullPath(TestSaveFolder));
                Environment.SetEnvironmentVariable(TestSaveVariable, Path.GetFullPath(TestSaveFolder));
            }
            else
            {
                EndTestSave();
                data["testSave"] = null;
                data["warning"] = "testSave is off: the game is playing against its REAL save.";
            }

            Driving = testSave;
            SessionState.SetBool(KeepRunningKey, keepRunning);
            data["keepRunning"] = testSave && keepRunning;

            try
            {
                SessionState.SetString(ManifestKey, Json.Serialize(RealSaveManifest(), false));
            }
            catch (Exception failure)
            {
                // Recorded as missing, which EndSession reports as unverified
                // rather than safe.
                SessionState.EraseString(ManifestKey);
                Debug.LogWarning("[ARIA] Could not list the real save before play: " + failure.Message);
            }

            SessionState.SetString(StartedAtKey, DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture));
            SessionState.SetString(SessionLogStartKey, LogNext.ToString(CultureInfo.InvariantCulture));
            SessionState.SetString(PendingStartKey,
                EditorApplication.timeSinceStartup.ToString("R", CultureInfo.InvariantCulture));

            EditorApplication.EnterPlaymode();

            return Ok("Entering play mode" + (testSave ? " on the test save" : " on the REAL save") +
                      ". A domain reload follows, so send the next command separately.", data);
        }

        #endregion

        #region Console log

        /// <summary>One console message, kept so a caller outside the editor can read it.</summary>
        private sealed class LogEntry
        {
            public long Seq;
            public string Type;
            public string Message;
            public string Stack;
            public int Frame;
            public bool Playing;
            public string At;
            public int Repeat = 1;
            public int LastFrame;
            public string LastAt;
        }

        private const int LogKept = 500;
        private const int FaultsKept = 200;
        private const int LogCarried = 300;
        private const int StackLines = 12;
        private const string LogStoreKey = "ARIA.Bridge.Log";
        private const string SessionLogStartKey = "ARIA.Bridge.SessionLogStart";

        /// <summary>What "errors" means: anything that is a fault, from the game or the compiler.</summary>
        private static readonly string[] ErrorTypes = { "error", "exception", "assert", "compileerror" };

        private static readonly string[] AllLogTypes =
            { "log", "warning", "error", "exception", "assert", "compile", "compileerror" };

        private static readonly object _logLock = new object();
        private static readonly List<LogEntry> _log = new List<LogEntry>();
        private static readonly List<LogEntry> _faults = new List<LogEntry>();
        private static long _logSeq;
        private static int _mainThread;
        private static int _compileErrors;

        /// <summary>The sequence number the next message will be given; "since" it, nothing has happened yet.</summary>
        private static long LogNext
        {
            get { lock (_logLock) { return _logSeq; } }
        }

        /// <summary>
        /// Every message the editor logs, the game's and Unity's alike.
        ///
        /// Several of the faults a person once had to find by playing were
        /// silent -- a null reference in a panel that simply never opened.
        /// They were in the console the whole time, where nothing outside the
        /// editor could see them. The threaded event, because a game logs
        /// from other threads too; frame and play state are only read on the
        /// main thread, where reading them is allowed.
        /// </summary>
        private static void OnLogMessage(string message, string stack, LogType type)
        {
            // The bridge's own progress lines are not the game's log.
            if (type == LogType.Log && message != null && message.StartsWith("[ARIA] ", StringComparison.Ordinal)) return;

            string kind = type == LogType.Exception ? "exception"
                        : type == LogType.Error ? "error"
                        : type == LogType.Assert ? "assert"
                        : type == LogType.Warning ? "warning"
                        : "log";

            bool main = System.Threading.Thread.CurrentThread.ManagedThreadId == _mainThread;
            bool fault = kind == "exception" || kind == "error" || kind == "assert";

            AddLog(kind, message, fault ? TrimStack(stack) : null,
                   main ? Time.frameCount : -1, main && EditorApplication.isPlaying);
        }

        private static void AddLog(string kind, string message, string stack, int frame, bool playing)
        {
            string text = message ?? "";
            string now = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);

            lock (_logLock)
            {
                _logSeq++;

                // The same message again, straight after itself, is one entry
                // counted again. Measured in the lab: a scene with two audio
                // listeners said so every frame, 500 times in under a second,
                // and the exception thrown in the middle of it was out of the
                // buffer before anything could ask for it.
                LogEntry last = _log.Count > 0 ? _log[_log.Count - 1] : null;
                if (last != null && last.Type == kind && last.Message == text && last.Stack == stack)
                {
                    last.Repeat++;
                    last.Seq = _logSeq;
                    last.LastFrame = frame;
                    last.LastAt = now;
                    return;
                }

                LogEntry entry = new LogEntry
                {
                    Seq = _logSeq,
                    Type = kind,
                    Message = text,
                    Stack = stack,
                    Frame = frame,
                    Playing = playing,
                    At = now,
                };

                _log.Add(entry);
                if (_log.Count > LogKept) _log.RemoveRange(0, _log.Count - LogKept);

                // Faults are kept apart as well, so no amount of ordinary
                // chatter, alternating or not, can push the one that matters out.
                if (IsError(kind))
                {
                    _faults.Add(entry);
                    if (_faults.Count > FaultsKept) _faults.RemoveRange(0, _faults.Count - FaultsKept);
                }
            }
        }

        /// <summary>
        /// Every kept entry, oldest first: the faults ordinary messages have
        /// pushed out of the log, then the log. Called holding the lock.
        /// </summary>
        private static List<LogEntry> KeptEntries()
        {
            List<LogEntry> all = new List<LogEntry>();
            long head = _log.Count > 0 ? _log[0].Seq : long.MaxValue;

            foreach (LogEntry fault in _faults)
            {
                if (fault.Seq < head) all.Add(fault);
            }

            all.AddRange(_log);
            return all;
        }

        private static string TrimStack(string stack)
        {
            if (string.IsNullOrEmpty(stack)) return null;

            string[] lines = stack.TrimEnd().Split('\n');
            int keep = Math.Min(lines.Length, StackLines);
            string kept = string.Join("\n", lines, 0, keep).TrimEnd();
            return lines.Length > StackLines ? kept + "\n..." : kept;
        }

        private static void OnCompilationStarted(object context)
        {
            _compileErrors = 0;
        }

        /// <summary>
        /// Compiler errors, which never reach the console as log messages.
        ///
        /// The case that matters most: a script that does not compile leaves
        /// the editor running the last scripts that did -- the bridge among
        /// them -- so the old bridge answers as if nothing were wrong. Kept
        /// here, it can say what was wrong instead.
        /// </summary>
        private static void OnAssemblyCompiled(string assembly, UnityEditor.Compilation.CompilerMessage[] messages)
        {
            if (messages == null) return;

            foreach (UnityEditor.Compilation.CompilerMessage message in messages)
            {
                if (message.type != UnityEditor.Compilation.CompilerMessageType.Error) continue;

                _compileErrors++;
                AddLog("compileerror", message.message, null, -1, false);
            }
        }

        private static void OnCompilationFinished(object context)
        {
            AddLog("compile", _compileErrors == 0
                ? "Compilation finished with no errors."
                : "Compilation finished with " + _compileErrors + " error(s). The editor keeps running the " +
                  "scripts that last compiled, this bridge among them, until they are fixed.", null, -1, false);
        }

        /// <summary>Carry the newest messages across a domain reload, which empties every static.</summary>
        private static void SaveLog()
        {
            List<object> carried = new List<object>();
            long seq;

            lock (_logLock)
            {
                seq = _logSeq;

                // Every fault, and the newest of everything else.
                List<LogEntry> all = KeptEntries();
                for (int index = 0; index < all.Count; index++)
                {
                    if (IsError(all[index].Type) || index >= all.Count - LogCarried)
                    {
                        carried.Add(DescribeLog(all[index], true));
                    }
                }
            }

            Dictionary<string, object> store = new Dictionary<string, object>();
            store["seq"] = seq;
            store["entries"] = carried;
            SessionState.SetString(LogStoreKey, Json.Serialize(store, false));
        }

        private static void RestoreLog()
        {
            string text = SessionState.GetString(LogStoreKey, "");
            if (string.IsNullOrEmpty(text)) return;

            object parsed;
            string error;
            Dictionary<string, object> store = Json.TryParse(text, out parsed, out error)
                ? parsed as Dictionary<string, object>
                : null;
            if (store == null) return;

            object raw;
            double number;

            lock (_logLock)
            {
                if (store.TryGetValue("seq", out raw) && TryNumber(raw, out number)) _logSeq = (long)number;

                List<object> entries = store.TryGetValue("entries", out raw) ? raw as List<object> : null;
                if (entries == null) return;

                _log.Clear();
                _faults.Clear();

                foreach (object item in entries)
                {
                    Dictionary<string, object> entry = item as Dictionary<string, object>;
                    if (entry == null) continue;

                    object stack;
                    LogEntry restored = new LogEntry
                    {
                        Seq = TryNumber(LogValue(entry, "seq"), out number) ? (long)number : 0,
                        Type = Text(LogValue(entry, "type")),
                        Message = Text(LogValue(entry, "message")),
                        Stack = entry.TryGetValue("stack", out stack) && stack != null ? Text(stack) : null,
                        Frame = TryNumber(LogValue(entry, "frame"), out number) ? (int)number : -1,
                        Playing = LogValue(entry, "playing") is bool && (bool)LogValue(entry, "playing"),
                        At = Text(LogValue(entry, "at")),
                        Repeat = TryNumber(LogValue(entry, "repeat"), out number) ? Math.Max(1, (int)number) : 1,
                        LastFrame = TryNumber(LogValue(entry, "lastFrame"), out number) ? (int)number : -1,
                        LastAt = LogValue(entry, "lastAt") != null ? Text(LogValue(entry, "lastAt")) : null,
                    };

                    _log.Add(restored);
                    if (IsError(restored.Type)) _faults.Add(restored);
                }

                if (_log.Count > LogKept) _log.RemoveRange(0, _log.Count - LogKept);
                if (_faults.Count > FaultsKept) _faults.RemoveRange(0, _faults.Count - FaultsKept);
            }
        }

        private static object LogValue(Dictionary<string, object> entry, string key)
        {
            object value;
            return entry.TryGetValue(key, out value) ? value : null;
        }

        private static Dictionary<string, object> DescribeLog(LogEntry entry, bool withStack)
        {
            Dictionary<string, object> described = new Dictionary<string, object>();
            described["seq"] = entry.Seq;
            described["type"] = entry.Type;
            described["message"] = entry.Message;
            if (withStack && entry.Stack != null) described["stack"] = entry.Stack;
            described["frame"] = entry.Frame;
            described["playing"] = entry.Playing;
            described["at"] = entry.At;

            if (entry.Repeat > 1)
            {
                described["repeat"] = entry.Repeat;
                described["lastFrame"] = entry.LastFrame;
                described["lastAt"] = entry.LastAt;
            }

            return described;
        }

        /// <summary>Where the log stood as the last bridge-started session began.</summary>
        private static long SessionLogStart()
        {
            long start;
            return long.TryParse(SessionState.GetString(SessionLogStartKey, "0"), NumberStyles.Integer,
                                 CultureInfo.InvariantCulture, out start) ? start : 0;
        }

        private static bool IsError(string type)
        {
            return Array.IndexOf(ErrorTypes, type) >= 0;
        }

        /// <summary>How many times something went wrong since a point in the log, repeats included.</summary>
        private static int ErrorsSince(long since)
        {
            int errors = 0;
            lock (_logLock)
            {
                foreach (LogEntry entry in _faults)
                {
                    if (entry.Seq > since) errors += entry.Repeat;
                }
            }
            return errors;
        }

        private static List<object> FirstErrorsSince(long since, int most)
        {
            List<object> first = new List<object>();
            lock (_logLock)
            {
                foreach (LogEntry entry in _faults)
                {
                    if (first.Count >= most) break;
                    if (entry.Seq <= since) continue;

                    Dictionary<string, object> described = DescribeLog(entry, false);
                    string line = entry.Message.Split('\n')[0];
                    described["message"] = line.Length > 300 ? line.Substring(0, 300) + "..." : line;
                    first.Add(described);
                }
            }
            return first;
        }

        /// <summary>Which types a GetLog asked for: null for all of them, empty when it named none that exist.</summary>
        private static HashSet<string> LogTypes(Dictionary<string, object> args)
        {
            object raw;
            if (!args.TryGetValue("types", out raw) && !args.TryGetValue("type", out raw)) return null;
            if (raw == null) return null;

            List<object> asked = new List<object>();
            IList many = raw as IList;
            if (many != null) foreach (object one in many) asked.Add(one);
            else foreach (string one in Text(raw).Split(',')) asked.Add(one);

            HashSet<string> types = new HashSet<string>(StringComparer.Ordinal);
            foreach (object one in asked)
            {
                string name = Normalise(Text(one));
                if (name.EndsWith("s", StringComparison.Ordinal) && name != "errors" &&
                    Array.IndexOf(AllLogTypes, name.Substring(0, name.Length - 1)) >= 0)
                {
                    name = name.Substring(0, name.Length - 1);
                }

                if (name == "errors") types.UnionWith(ErrorTypes);
                else if (Array.IndexOf(AllLogTypes, name) >= 0) types.Add(name);
            }
            return types;
        }

        /// <summary>
        /// What the console has said: the game's logs, warnings, errors and
        /// exceptions, and the compiler's errors, oldest first.
        ///
        /// since takes the "next" a previous call returned, so a caller reads
        /// only what is new; session:true starts from the last bridge-started
        /// play session. types narrows it ("errors" is every kind of fault),
        /// contains searches the text, limit keeps the newest. Errors carry
        /// the first lines of their stack. The newest 500 are kept, and the
        /// newest 300 survive a domain reload.
        /// </summary>
        private static CommandResult GetLog(Dictionary<string, object> args, Scope scope)
        {
            long since = (long)Num(args, 0, "since");
            if (Bool(args, "session", false)) since = Math.Max(since, SessionLogStart());

            int limit = Math.Max(1, Math.Min(LogKept, (int)Num(args, 100, "limit")));
            bool withStack = Bool(args, "stack", true);
            string contains = Str(args, "contains");

            HashSet<string> types = LogTypes(args);
            if (types != null && types.Count == 0)
            {
                return Fail("types named none of: log, warning, error, exception, assert, compile, " +
                            "compileError, or errors for every kind of fault.");
            }

            List<LogEntry> picked = new List<LogEntry>();
            Dictionary<string, object> counts = new Dictionary<string, object>();
            long next;
            long oldest;

            lock (_logLock)
            {
                next = _logSeq;
                List<LogEntry> kept = KeptEntries();
                oldest = kept.Count > 0 ? kept[0].Seq : next + 1;

                foreach (LogEntry entry in kept)
                {
                    if (entry.Seq <= since) continue;

                    // Counted by occurrence, so a message said a thousand
                    // times reads as a thousand, not as one.
                    object seen;
                    counts[entry.Type] = (counts.TryGetValue(entry.Type, out seen) ? (int)seen : 0) + entry.Repeat;

                    if (types != null && !types.Contains(entry.Type)) continue;
                    if (!string.IsNullOrEmpty(contains) &&
                        entry.Message.IndexOf(contains, StringComparison.OrdinalIgnoreCase) < 0) continue;

                    picked.Add(entry);
                }
            }

            int skipped = Math.Max(0, picked.Count - limit);
            List<object> entries = new List<object>();
            for (int index = skipped; index < picked.Count; index++) entries.Add(DescribeLog(picked[index], withStack));

            int errors = 0;
            foreach (string type in ErrorTypes)
            {
                object count;
                if (counts.TryGetValue(type, out count)) errors += (int)count;
            }

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["entries"] = entries;
            data["since"] = since;
            data["next"] = next;
            data["counts"] = counts;
            data["errors"] = errors;
            data["skipped"] = skipped;

            // Asking from before the oldest kept message cannot be answered in
            // full; saying so is better than a list that looks complete.
            if (since + 1 < oldest) data["dropped"] = "Messages before " + oldest + " are no longer kept.";

            return Ok(entries.Count + " message(s), " + errors + " error(s) since " + since + ".", data);
        }

        #endregion

        #region Commands: playing the game

        /// <summary>
        /// Refuse a command that plays the game unless the game is a test.
        ///
        /// Keys, clicks and console commands change the game, and the game
        /// saves what changes. On the test save that is the point; on the
        /// player's real save it is how hours of progress become a test's
        /// leftovers. allowRealSave says the caller means it.
        /// </summary>
        private static CommandResult RefuseUnlessDriving(Dictionary<string, object> args, string command)
        {
            if (!EditorApplication.isPlaying) return Fail(command + " needs the game playing. SetPlayMode first.");
            if (Driving || Bool(args, "allowRealSave", false)) return null;

            return Fail(command + " drives only a test session -- one SetPlayMode started on the test save -- " +
                        "because what it does changes the game, and the game saves what changes. This session " +
                        "is playing on the real save. Pass allowRealSave:true to do it anyway.");
        }

        /// <summary>How many input steps are still waiting to be played.</summary>
        private static int PendingInput
        {
            get
            {
#if ENABLE_INPUT_SYSTEM
                return _input.Count;
#else
                return 0;
#endif
            }
        }

        /// <summary>
        /// Play queued input a step at a time as the game's frames go by.
        ///
        /// Every editor update, not every half second like Poll: a key held
        /// for two frames has to be let go on the third, not a dozen later.
        /// </summary>
        private static void Pump()
        {
            PumpBurst();

#if ENABLE_INPUT_SYSTEM
            if (_input.Count == 0) return;

            if (!EditorApplication.isPlaying)
            {
                _input.Clear();
                return;
            }

            int frame = Time.frameCount;
            double now = EditorApplication.timeSinceStartup;

            // Several steps may fall due in one update -- a move and the press
            // after it -- and a cap keeps a long queue from running away with
            // a single frame.
            for (int guard = 0; guard < 64 && _input.Count > 0; guard++)
            {
                InputStep next = _input[0];
                if (frame < _inputFrame + next.Frames) break;
                if (now < _inputTime + next.Seconds) break;

                _input.RemoveAt(0);
                _inputFrame = frame;
                _inputTime = now;

                try
                {
                    next.Run();
                }
                catch (Exception failure)
                {
                    Debug.LogWarning("[ARIA] Input step '" + next.Label + "' failed: " + failure.Message);
                }
            }
#endif
        }

        /// <summary>
        /// Press keys, move and click the mouse, scroll and type, in a game the bridge started.
        ///
        /// Through two devices of the bridge's own, "ARIA Keyboard" and "ARIA
        /// Mouse", which become Keyboard.current and Mouse.current as they
        /// send -- so a game reading either, and the UI's input module, sees
        /// them exactly as it sees hardware, and the real devices are left
        /// alone. Queued and played over the next frames: a press and its
        /// release have to land in different frames for wasPressedThisFrame
        /// to see them. Ping's inputPending reaches 0 when it is all done.
        /// </summary>
        private static CommandResult SendInput(Dictionary<string, object> args, Scope scope)
        {
            if (scope.InPrefab) return Fail("SendInput is not allowed inside ModifyPrefab.");

            CommandResult refused = RefuseUnlessDriving(args, "SendInput");
            if (refused != null) return refused;

#if ENABLE_INPUT_SYSTEM
            List<object> actions;
            object raw;
            if (args.TryGetValue("actions", out raw) && raw is List<object>) actions = (List<object>)raw;
            else actions = new List<object> { args };

            if (actions.Count == 0) return Fail("SendInput needs at least one action.");

            List<InputStep> steps = new List<InputStep>();
            List<object> planned = new List<object>();

            for (int index = 0; index < actions.Count; index++)
            {
                Dictionary<string, object> action = actions[index] as Dictionary<string, object>;
                if (action == null) return Fail("Action " + (index + 1) + " is not an object.");

                int first = steps.Count;

                string error;
                if (!PlanAction(action, scope, steps, planned, out error))
                {
                    return Fail("Action " + (index + 1) + ": " + error + ". Nothing was queued.");
                }

                // A frame between one action and the next, whatever they are.
                // Measured in the lab: two clicks on a button followed at once
                // by a click on a cube lost the second button click every time.
                // Its release and the move away to the cube went out in one
                // editor update, the game read both in one input update, and
                // the UI module saw the button let go with the pointer
                // elsewhere -- which is not a click.
                bool follows = first > 0 || _input.Count > 0;
                if (follows && first < steps.Count) steps[first].Frames = Math.Max(1, steps[first].Frames);
            }

            // Timed from now when nothing is waiting, so an idle queue's old
            // timestamp cannot make the first step look overdue.
            if (_input.Count == 0)
            {
                _inputFrame = Time.frameCount;
                _inputTime = EditorApplication.timeSinceStartup;
            }

            _input.AddRange(steps);

            int frames = 0;
            foreach (InputStep step in steps) frames += step.Frames;

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["queued"] = steps.Count;
            data["pending"] = _input.Count;
            data["frame"] = Time.frameCount;
            data["framesNeeded"] = frames;
            data["screen"] = UiPoint(GameScreenSize());
            data["actions"] = planned;
            return Ok("Queued " + steps.Count + " input step(s) over about " + frames +
                      " frame(s). Ping's inputPending reaches 0 when they have played.", data);
#else
            return Fail("SendInput feeds the Input System package, and this project does not use it: Active " +
                        "Input Handling is the old Input Manager, whose Input.* reads the hardware and cannot be fed.");
#endif
        }

        /// <summary>
        /// Everything visible on the game's screen that a person would read or press.
        ///
        /// Text (UI Text and TextMeshPro) and controls (anything that is a
        /// Selectable: buttons, toggles, sliders, fields), each with where it
        /// is drawn in screen pixels from the bottom left -- the same pixels
        /// SendInput clicks at. "lines" is the visible text in reading order,
        /// which is most of what a screenshot would have been asked for.
        /// Visible means active, enabled, not faded out by a CanvasGroup, not
        /// transparent, and on the screen; includeHidden lists the rest too.
        /// targets asks where particular objects are drawn, UI or world.
        ///
        /// The UI types are found by name, not referenced, so the bridge still
        /// compiles in a project without uGUI or TextMeshPro. IMGUI (OnGUI)
        /// draws nothing that can be read back; a screenshot is the only way
        /// to see it.
        /// </summary>
        private static CommandResult ReadScreen(Dictionary<string, object> args, Scope scope)
        {
            if (scope.InPrefab) return Fail("ReadScreen is not allowed inside ModifyPrefab.");

            bool includeHidden = Bool(args, "includeHidden", false);
            int limit = Math.Max(1, (int)Num(args, 200, "limit"));
            Vector2 size = GameScreenSize();

            List<object> texts = new List<object>();
            List<object> controls = new List<object>();
            List<KeyValuePair<Vector2, string>> seen = new List<KeyValuePair<Vector2, string>>();

            foreach (Canvas canvas in FindAll<Canvas>())
            {
                if (canvas == null || !canvas.isRootCanvas) continue;

                foreach (Component part in canvas.GetComponentsInChildren<Component>(true))
                {
                    if (part == null) continue;

                    Type type = part.GetType();
                    bool isText = UiIsText(type);
                    bool isControl = !isText && UiIsA(type, "UnityEngine.UI.Selectable");
                    if (!isText && !isControl) continue;

                    RectTransform rect = part.transform as RectTransform;
                    if (rect == null) continue;

                    Rect area = ScreenRect(rect, canvas);
                    bool onScreen = area.xMax > 0f && area.yMax > 0f && area.xMin < size.x && area.yMin < size.y;
                    bool visible = onScreen && canvas.isActiveAndEnabled && UiShown(part);
                    if (!visible && !includeHidden) continue;

                    Dictionary<string, object> entry = new Dictionary<string, object>();
                    entry["path"] = HierarchyPath(part.transform);
                    entry["kind"] = type.Name;
                    entry["center"] = UiPoint(area.center);
                    entry["rect"] = new List<object> { UiRound(area.x), UiRound(area.y), UiRound(area.width), UiRound(area.height) };
                    entry["visible"] = visible;

                    if (isText)
                    {
                        string words = Convert.ToString(UiMember(part, "text"), CultureInfo.InvariantCulture);
                        if (string.IsNullOrEmpty(words) || words.Trim().Length == 0) continue;

                        entry["text"] = words;
                        if (texts.Count < limit) texts.Add(entry);
                        if (visible) seen.Add(new KeyValuePair<Vector2, string>(area.center, UiPlain(words)));
                    }
                    else
                    {
                        entry["interactable"] = UiMember(part, "interactable");

                        string label = UiLabel(part);
                        if (label != null) entry["label"] = label;

                        foreach (string name in new[] { "isOn", "value", "text" })
                        {
                            object value = UiMember(part, name);
                            if (value is bool || value is float || value is int || value is string) entry[name] = value;
                        }

                        if (controls.Count < limit) controls.Add(entry);
                    }
                }
            }

            // Reading order: rows from the top of the screen down, and left to
            // right within a row, a row being anything within a few pixels.
            seen.Sort(delegate (KeyValuePair<Vector2, string> a, KeyValuePair<Vector2, string> b)
            {
                int rowA = Mathf.RoundToInt(a.Key.y / 12f);
                int rowB = Mathf.RoundToInt(b.Key.y / 12f);
                return rowA != rowB ? rowB.CompareTo(rowA) : a.Key.x.CompareTo(b.Key.x);
            });

            List<object> lines = new List<object>();
            foreach (KeyValuePair<Vector2, string> line in seen) lines.Add(line.Value);

            Dictionary<string, object> data = new Dictionary<string, object>();
            data["screen"] = UiPoint(size);
            data["isPlaying"] = EditorApplication.isPlaying;
            data["lines"] = lines;
            data["texts"] = texts;
            data["controls"] = controls;

            object wanted;
            if (args.TryGetValue("targets", out wanted) && wanted != null)
            {
                IList list = (wanted as IList) ?? new List<object> { wanted };
                List<object> found = new List<object>();

                foreach (object one in list)
                {
                    string reference = Text(one);
                    Dictionary<string, object> described = new Dictionary<string, object>();
                    described["target"] = reference;

                    string error;
                    Vector2 point;
                    GameObject target = Resolve(reference, scope, out error);

                    if (target != null && TryScreenPointOf(target, out point, out error))
                    {
                        described["found"] = true;
                        described["path"] = HierarchyPath(target.transform);
                        described["center"] = UiPoint(point);
                        described["onScreen"] = point.x >= 0f && point.y >= 0f && point.x <= size.x && point.y <= size.y;
                        described["active"] = target.activeInHierarchy;
                    }
                    else
                    {
                        described["found"] = false;
                        described["error"] = error;
                    }

                    found.Add(described);
                }

                data["targets"] = found;
            }

            if (!EditorApplication.isPlaying && texts.Count == 0 && controls.Count == 0)
            {
                data["note"] = "Nothing is playing and no interface was found. A game that builds its interface " +
                               "at runtime has none until it runs: SetPlayMode first.";
            }

            return Ok(lines.Count + " visible line(s) of text, " + controls.Count + " control(s).", data);
        }

        /// <summary>The size of the picture the game draws, in the pixels its pointer positions use.</summary>
        private static Vector2 GameScreenSize()
        {
            // A root overlay canvas is sized to the game view exactly; a camera
            // is the next best measure, and Screen the last, because from an
            // editor callback Screen can describe whichever window is drawing.
            foreach (Canvas canvas in FindAll<Canvas>())
            {
                if (canvas == null || !canvas.isActiveAndEnabled || !canvas.isRootCanvas) continue;
                if (canvas.renderMode != RenderMode.ScreenSpaceOverlay) continue;

                Rect area = canvas.pixelRect;
                if (area.width > 1f && area.height > 1f) return area.size;
            }

            Camera camera = Camera.main;
            if (camera != null) return new Vector2(camera.pixelWidth, camera.pixelHeight);
            return new Vector2(Screen.width, Screen.height);
        }

        /// <summary>Where a RectTransform is drawn, in screen pixels from the bottom left.</summary>
        private static Rect ScreenRect(RectTransform rect, Canvas canvas)
        {
            Vector3[] corners = new Vector3[4];
            rect.GetWorldCorners(corners);

            // An overlay canvas's world space IS screen space; any other kind
            // is drawn by a camera, and has to be put through it.
            Canvas root = canvas.rootCanvas;
            Camera camera = root.renderMode == RenderMode.ScreenSpaceOverlay
                ? null
                : (root.worldCamera != null ? root.worldCamera : Camera.main);

            float minX = float.MaxValue, minY = float.MaxValue, maxX = float.MinValue, maxY = float.MinValue;
            for (int index = 0; index < 4; index++)
            {
                Vector2 point = RectTransformUtility.WorldToScreenPoint(camera, corners[index]);
                minX = Mathf.Min(minX, point.x);
                minY = Mathf.Min(minY, point.y);
                maxX = Mathf.Max(maxX, point.x);
                maxY = Mathf.Max(maxY, point.y);
            }

            return Rect.MinMaxRect(minX, minY, maxX, maxY);
        }

        /// <summary>
        /// Where an object is drawn: a UI element's centre, or a world object's
        /// middle seen through the main camera.
        /// </summary>
        private static bool TryScreenPointOf(GameObject target, out Vector2 point, out string error)
        {
            point = Vector2.zero;
            error = null;

            RectTransform rect = target.transform as RectTransform;
            Canvas canvas = target.GetComponentInParent<Canvas>();
            if (rect != null && canvas != null)
            {
                point = ScreenRect(rect, canvas).center;
                return true;
            }

            Camera camera = Camera.main;
            if (camera == null)
            {
                foreach (Camera each in FindAll<Camera>())
                {
                    if (each != null && each.isActiveAndEnabled) { camera = each; break; }
                }
            }
            if (camera == null)
            {
                error = "there is no camera to see '" + target.name + "' through";
                return false;
            }

            // The renderer's middle rather than the pivot: a rock's pivot can
            // sit at its base, and clicking the floor under it misses it.
            Renderer shape = target.GetComponentInChildren<Renderer>();
            Vector3 world = shape != null ? shape.bounds.center : target.transform.position;
            Vector3 screen = camera.WorldToScreenPoint(world);

            if (screen.z <= 0f)
            {
                error = "'" + target.name + "' is behind the camera";
                return false;
            }

            point = new Vector2(screen.x, screen.y);
            return true;
        }

        /// <summary>
        /// A place on the screen: [x, y] pixels from the bottom left, [x, y]
        /// from 0 to 1 when space is "viewport", or an object's path.
        /// </summary>
        private static bool TryPoint(object where, string space, Scope scope, out Vector2 point, out string error)
        {
            point = Vector2.zero;
            error = null;

            string reference = where as string;
            if (reference != null)
            {
                GameObject target = Resolve(reference, scope, out error);
                if (target == null) return false;
                return TryScreenPointOf(target, out point, out error);
            }

            Vector2 xy;
            if (!TryXY(where, out xy))
            {
                error = "a place is [x, y] in screen pixels from the bottom left, or an object's path";
                return false;
            }

            if (Normalise(space) == "viewport")
            {
                Vector2 size = GameScreenSize();
                xy = new Vector2(xy.x * size.x, xy.y * size.y);
            }

            point = xy;
            return true;
        }

        private static bool TryXY(object value, out Vector2 xy)
        {
            xy = Vector2.zero;
            double x, y;

            IList list = value as IList;
            if (list != null && list.Count >= 2 && TryNumber(list[0], out x) && TryNumber(list[1], out y))
            {
                xy = new Vector2((float)x, (float)y);
                return true;
            }

            Dictionary<string, object> map = value as Dictionary<string, object>;
            object rawX, rawY;
            if (map != null && map.TryGetValue("x", out rawX) && map.TryGetValue("y", out rawY) &&
                TryNumber(rawX, out x) && TryNumber(rawY, out y))
            {
                xy = new Vector2((float)x, (float)y);
                return true;
            }

            return false;
        }

        private static bool UiIsA(Type type, string fullName)
        {
            for (Type each = type; each != null; each = each.BaseType)
            {
                if (each.FullName == fullName) return true;
            }
            return false;
        }

        private static bool UiIsText(Type type)
        {
            return UiIsA(type, "UnityEngine.UI.Text") || UiIsA(type, "TMPro.TMP_Text");
        }

        /// <summary>A public property or field by name, or null; never throws.</summary>
        private static object UiMember(object target, string name)
        {
            if (target == null) return null;

            try
            {
                PropertyInfo property = target.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.Instance);
                if (property != null && property.CanRead && property.GetIndexParameters().Length == 0)
                {
                    return property.GetValue(target, null);
                }

                FieldInfo field = target.GetType().GetField(name, BindingFlags.Public | BindingFlags.Instance);
                return field != null ? field.GetValue(target) : null;
            }
            catch (Exception)
            {
                // Ambiguous overloads and throwing getters alike: an unreadable
                // member is reported as absent, not as a failed command.
                return null;
            }
        }

        /// <summary>Whether a piece of UI would be seen, leaving aside where it is.</summary>
        private static bool UiShown(Component part)
        {
            if (!part.gameObject.activeInHierarchy) return false;

            Behaviour behaviour = part as Behaviour;
            if (behaviour != null && !behaviour.enabled) return false;

            Canvas nearest = part.GetComponentInParent<Canvas>();
            if (nearest != null && !nearest.enabled) return false;

            float alpha = 1f;
            for (Transform each = part.transform; each != null; each = each.parent)
            {
                CanvasGroup group = each.GetComponent<CanvasGroup>();
                if (group == null || !group.enabled) continue;

                alpha *= group.alpha;
                if (group.ignoreParentGroups) break;
            }
            if (alpha < 0.01f) return false;

            object color = UiMember(part, "color");
            return !(color is Color) || ((Color)color).a >= 0.01f;
        }

        /// <summary>The first text inside a control, which is what a person would call it.</summary>
        private static string UiLabel(Component control)
        {
            foreach (Component part in control.GetComponentsInChildren<Component>(false))
            {
                if (part == null || !UiIsText(part.GetType())) continue;

                string words = Convert.ToString(UiMember(part, "text"), CultureInfo.InvariantCulture);
                if (!string.IsNullOrEmpty(words) && words.Trim().Length > 0) return UiPlain(words);
            }
            return null;
        }

        /// <summary>Text without rich-text tags, as it reads on screen.</summary>
        private static string UiPlain(string words)
        {
            return Regex.Replace(words ?? "", "<[^>]*>", "").Trim();
        }

        private static List<object> UiPoint(Vector2 point)
        {
            return new List<object> { UiRound(point.x), UiRound(point.y) };
        }

        private static double UiRound(float value)
        {
            return Math.Round(value, 1);
        }

#if ENABLE_INPUT_SYSTEM
        /// <summary>One thing SendInput does, and how long after the step before it.</summary>
        private sealed class InputStep
        {
            public int Frames;
            public double Seconds;
            public string Label;
            public Action Run;
        }

        private static readonly List<InputStep> _input = new List<InputStep>();
        private static int _inputFrame;
        private static double _inputTime;

        private static readonly HashSet<Key> _heldKeys = new HashSet<Key>();
        private static Vector2 _pointer;
        private static ushort _buttons;
        private static Keyboard _ariaKeyboard;
        private static Mouse _ariaMouse;

        /// <summary>Every device the bridge adds is named with this, and only those are ever removed.</summary>
        private const string AriaDevicePrefix = "ARIA ";

        private const string DrivenInputSettingsName = "ARIA Driven Input Settings";
        private static InputSettings _originalInputSettings;
        private static InputSettings _drivenInputSettings;

        private static void Step(List<InputStep> steps, int frames, double seconds, string label, Action run)
        {
            steps.Add(new InputStep { Frames = frames, Seconds = seconds, Label = label, Run = run });
        }

        /// <summary>
        /// Turn one action into steps.
        ///
        ///   {"key": "space"}                     tap; hold? frames (2), times?, action? tap/down/up
        ///   {"key": ["LeftCtrl", "s"]}           held together
        ///   {"click": [640, 360]}                button? left/right/middle, hold?, times?, space? viewport
        ///   {"click": "Canvas/BuyButton"}        where that object is drawn
        ///   {"move": [x, y] | "path"}
        ///   {"mouse": "down" | "up", "at"?: ..}  button?
        ///   {"scroll": -120}  or [x, y]          up is positive
        ///   {"text": "hello"}                    Keyboard.onTextInput; IMGUI fields do not hear it
        ///   {"wait": 30} / {"waitSeconds": 0.5}
        ///
        /// Places are found as the command arrives: something that only
        /// appears later is clicked after a wait sent as its own command.
        /// </summary>
        private static bool PlanAction(Dictionary<string, object> action, Scope scope,
                                       List<InputStep> steps, List<object> planned, out string error)
        {
            error = null;

            int hold = Math.Max(1, (int)Num(action, 2, "hold", "holdFrames"));
            double holdSeconds = Math.Max(0, Num(action, 0, "holdSeconds"));
            int times = Math.Max(1, Math.Min(1000, (int)Num(action, 1, "times", "repeat")));
            string how = Normalise(Str(action, "action", "state") ?? "tap");

            Dictionary<string, object> said = new Dictionary<string, object>();

            if (action.ContainsKey("wait") || action.ContainsKey("waitFrames") || action.ContainsKey("waitSeconds"))
            {
                int frames = Math.Max(0, (int)Num(action, 0, "wait", "waitFrames"));
                double seconds = Math.Max(0, Num(action, 0, "waitSeconds"));
                Step(steps, frames, seconds, "wait", delegate { });

                said["wait"] = frames;
                if (seconds > 0) said["waitSeconds"] = seconds;
                planned.Add(said);
                return true;
            }

            object keyValue;
            if (action.TryGetValue("key", out keyValue) || action.TryGetValue("keys", out keyValue))
            {
                List<Key> keys = new List<Key>();
                List<object> names = new List<object>();
                IList many = keyValue as IList;
                if (many != null) foreach (object one in many) names.Add(one);
                else names.Add(keyValue);

                foreach (object one in names)
                {
                    Key parsed;
                    if (!TryKey(Text(one), out parsed))
                    {
                        error = "there is no key called '" + Text(one) + "'. Keys use the Input System's names: " +
                                "Space, Enter, Escape, A, Digit1, LeftShift, UpArrow, F5, Backquote";
                        return false;
                    }
                    keys.Add(parsed);
                }

                if (keys.Count == 0)
                {
                    error = "key names no key";
                    return false;
                }

                if (how != "tap" && how != "press" && how != "down" && how != "up")
                {
                    error = "a key's action is tap, down or up, not '" + how + "'";
                    return false;
                }

                Key[] chord = keys.ToArray();
                string label = string.Join("+", Array.ConvertAll(chord, key => key.ToString()));

                for (int round = 0; round < times; round++)
                {
                    int gap = round == 0 ? 0 : 1;

                    if (how == "up")
                    {
                        Step(steps, gap, 0, label + " up", delegate { SetKeys(chord, false); });
                        continue;
                    }

                    Step(steps, gap, 0, label + " down", delegate { SetKeys(chord, true); });
                    if (how == "down") continue;

                    Step(steps, hold, holdSeconds, label + " up", delegate { SetKeys(chord, false); });
                }

                said["key"] = label;
                said["action"] = how == "press" ? "tap" : how;
                said["times"] = times;
                planned.Add(said);
                return true;
            }

            if (action.ContainsKey("text"))
            {
                string words = Str(action, "text") ?? "";
                Step(steps, 0, 0, "text", delegate { TypeText(words); });

                said["text"] = words;
                planned.Add(said);
                return true;
            }

            object scrollValue;
            if (action.TryGetValue("scroll", out scrollValue))
            {
                Vector2 amount;
                double single;
                if (TryNumber(scrollValue, out single)) amount = new Vector2(0f, (float)single);
                else if (!TryXY(scrollValue, out amount))
                {
                    error = "scroll is a number (up is positive) or [x, y]";
                    return false;
                }

                // A scroll is a delta for one frame; the event after it puts
                // the wheel back at rest, or it would keep scrolling.
                Vector2 wheel = amount;
                Step(steps, 0, 0, "scroll", delegate { SetMouse(null, -1, false, wheel); });
                Step(steps, 1, 0, "scroll end", delegate { SetMouse(null, -1, false, Vector2.zero); });

                said["scroll"] = new List<object> { (double)amount.x, (double)amount.y };
                planned.Add(said);
                return true;
            }

            bool click = action.ContainsKey("click");
            bool move = action.ContainsKey("move");
            bool mouse = action.ContainsKey("mouse");

            if (click || move || mouse)
            {
                object where = click ? action["click"] : move ? action["move"] : null;
                if (where == null || where is bool)
                {
                    object at;
                    where = action.TryGetValue("at", out at) ? at : null;
                }

                Vector2? point = null;
                if (where != null)
                {
                    Vector2 found;
                    if (!TryPoint(where, Str(action, "space"), scope, out found, out error)) return false;

                    point = found;
                    said["at"] = UiPoint(found);
                    if (where is string) said["target"] = where;
                }

                int button;
                string buttonName = Str(action, "button") ?? "left";
                if (!TryMouseButton(buttonName, out button))
                {
                    error = "button is left, right, middle, back or forward, not '" + buttonName + "'";
                    return false;
                }

                if (move)
                {
                    if (point == null)
                    {
                        error = "move needs a place: [x, y] or an object's path";
                        return false;
                    }

                    Vector2 to = point.Value;
                    Step(steps, 0, 0, "move", delegate { SetMouse(to, -1, false, Vector2.zero); });

                    said["move"] = true;
                    planned.Add(said);
                    return true;
                }

                if (click)
                {
                    if (point == null)
                    {
                        error = "click needs a place: [x, y] or an object's path";
                        return false;
                    }

                    // Arrive, then press a frame later, then let go: the UI's
                    // input module wants the pointer over a button before it
                    // will count a press on it.
                    Vector2 to = point.Value;
                    int which = button;
                    for (int round = 0; round < times; round++)
                    {
                        Step(steps, round == 0 ? 0 : 1, 0, "move", delegate { SetMouse(to, -1, false, Vector2.zero); });
                        Step(steps, 1, 0, buttonName + " press", delegate { SetMouse(to, which, true, Vector2.zero); });
                        Step(steps, hold, holdSeconds, buttonName + " release", delegate { SetMouse(to, which, false, Vector2.zero); });
                    }

                    said["click"] = buttonName;
                    said["times"] = times;
                    planned.Add(said);
                    return true;
                }

                string state = Normalise(Text(action["mouse"]));
                if (state != "down" && state != "up")
                {
                    error = "mouse is down or up, not '" + state + "'";
                    return false;
                }

                bool pressing = state == "down";
                Vector2? place = point;
                int pressed = button;
                Step(steps, 0, 0, "mouse " + state, delegate { SetMouse(place, pressed, pressing, Vector2.zero); });

                said["mouse"] = state;
                said["button"] = buttonName;
                planned.Add(said);
                return true;
            }

            error = "an action needs one of key, text, click, move, mouse, scroll or wait";
            return false;
        }

        /// <summary>A key from a name a person would write, or the Input System's own.</summary>
        private static bool TryKey(string name, out Key key)
        {
            key = Key.None;
            if (string.IsNullOrEmpty(name)) return false;

            if (name.Length == 1)
            {
                switch (name[0])
                {
                    case ' ': key = Key.Space; return true;
                    case '`': case '~': key = Key.Backquote; return true;
                    case ',': key = Key.Comma; return true;
                    case '.': key = Key.Period; return true;
                    case '/': key = Key.Slash; return true;
                    case '\\': key = Key.Backslash; return true;
                    case ';': key = Key.Semicolon; return true;
                    case '\'': key = Key.Quote; return true;
                    case '[': key = Key.LeftBracket; return true;
                    case ']': key = Key.RightBracket; return true;
                    case '-': key = Key.Minus; return true;
                    case '=': key = Key.Equals; return true;
                }

                if (char.IsDigit(name[0])) return TryEnum("Digit" + name, out key);
            }

            switch (Normalise(name))
            {
                case "esc": key = Key.Escape; return true;
                case "return": key = Key.Enter; return true;
                case "ctrl": case "control": key = Key.LeftCtrl; return true;
                case "shift": key = Key.LeftShift; return true;
                case "alt": key = Key.LeftAlt; return true;
                case "up": key = Key.UpArrow; return true;
                case "down": key = Key.DownArrow; return true;
                case "left": key = Key.LeftArrow; return true;
                case "right": key = Key.RightArrow; return true;
                case "del": key = Key.Delete; return true;
                case "pgup": key = Key.PageUp; return true;
                case "pgdn": case "pagedn": key = Key.PageDown; return true;
                case "win": case "cmd": case "meta": key = Key.LeftMeta; return true;
                case "backtick": case "grave": case "tilde": key = Key.Backquote; return true;
                case "spacebar": key = Key.Space; return true;
            }

            return TryEnum(name, out key) && key != Key.None;
        }

        /// <summary>MouseButton's numbering: the bit each button has in MouseState.buttons.</summary>
        private static bool TryMouseButton(string name, out int button)
        {
            switch (Normalise(name))
            {
                case "": case "left": button = 0; return true;
                case "right": button = 1; return true;
                case "middle": button = 2; return true;
                case "forward": button = 3; return true;
                case "back": button = 4; return true;
            }

            button = 0;
            return false;
        }

        private static Keyboard AriaKeyboard()
        {
            if (_ariaKeyboard == null || !_ariaKeyboard.added)
            {
                _ariaKeyboard = InputSystem.AddDevice<Keyboard>(AriaDevicePrefix + "Keyboard");
            }
            return _ariaKeyboard;
        }

        private static Mouse AriaMouse()
        {
            if (_ariaMouse == null || !_ariaMouse.added)
            {
                _ariaMouse = InputSystem.AddDevice<Mouse>(AriaDevicePrefix + "Mouse");
            }
            return _ariaMouse;
        }

        /// <summary>Press or let go of keys; every event carries the whole keyboard, so the held set is kept here.</summary>
        private static void SetKeys(Key[] keys, bool down)
        {
            foreach (Key key in keys)
            {
                if (down) _heldKeys.Add(key);
                else _heldKeys.Remove(key);
            }

            Key[] held = new Key[_heldKeys.Count];
            _heldKeys.CopyTo(held);

            Keyboard board = AriaKeyboard();
            InputSystem.QueueStateEvent(board, new KeyboardState(held));
            board.MakeCurrent();
        }

        private static void TypeText(string words)
        {
            Keyboard board = AriaKeyboard();
            foreach (char character in words) InputSystem.QueueTextEvent(board, character);
            board.MakeCurrent();
        }

        /// <summary>Move, press, release or scroll; every event carries the whole mouse.</summary>
        private static void SetMouse(Vector2? at, int button, bool down, Vector2 scroll)
        {
            Vector2 delta = Vector2.zero;
            if (at.HasValue)
            {
                delta = at.Value - _pointer;
                _pointer = at.Value;
            }

            if (button >= 0)
            {
                int bit = 1 << button;
                _buttons = (ushort)(down ? (_buttons | bit) : (_buttons & ~bit));
            }

            MouseState state = new MouseState();
            state.position = _pointer;
            state.delta = delta;
            state.scroll = scroll;
            state.buttons = _buttons;

            Mouse mouse = AriaMouse();
            InputSystem.QueueStateEvent(mouse, state);
            mouse.MakeCurrent();
        }

        /// <summary>
        /// Let input reach the game however focus lies, for a test session.
        ///
        /// The Input System's editor default sends keyboard and pointer input
        /// to the game only while the Game view has focus, and holds it for
        /// the editor otherwise -- read in InputManager: with the Game view
        /// unfocused, a keyboard or pointer event is left for an editor update
        /// and the game never sees it. A test is sent precisely while the
        /// person is elsewhere, so for its length the settings say input
        /// always goes to the game and focus is ignored. A copy is changed,
        /// never the project's settings object, and the original is put back
        /// as play ends.
        /// </summary>
        private static void RouteInputToGame()
        {
            InputSettings current = InputSystem.settings;
            if (current == null || current.name == DrivenInputSettingsName) return;

            InputSettings driven = Object.Instantiate(current);
            driven.name = DrivenInputSettingsName;
            driven.hideFlags = HideFlags.HideAndDontSave;
            driven.backgroundBehavior = InputSettings.BackgroundBehavior.IgnoreFocus;
            driven.editorInputBehaviorInPlayMode = InputSettings.EditorInputBehaviorInPlayMode.AllDeviceInputAlwaysGoesToGameView;

            _originalInputSettings = current;
            _drivenInputSettings = driven;
            InputSystem.settings = driven;
        }

        private static void RestoreInput()
        {
            RemoveAriaDevices();

            if (_originalInputSettings != null && InputSystem.settings == _drivenInputSettings)
            {
                InputSystem.settings = _originalInputSettings;
            }

            if (_drivenInputSettings != null) Object.DestroyImmediate(_drivenInputSettings);
            _originalInputSettings = null;
            _drivenInputSettings = null;
        }

        /// <summary>
        /// Put the project's settings back if a session ended without doing
        /// it -- a reload in the middle of play loses the reference to them.
        /// </summary>
        private static void RecoverInputSettings()
        {
            InputSettings current = InputSystem.settings;
            if (current == null || current.name != DrivenInputSettingsName) return;

            InputSettings asset;
            bool fromProject = EditorBuildSettings.TryGetConfigObject("com.unity.input.settings", out asset) && asset != null;
            InputSystem.settings = fromProject ? asset : ScriptableObject.CreateInstance<InputSettings>();

            Debug.LogWarning("[ARIA] Put the Input System settings back after a test session that could not.");
        }

        /// <summary>Take away every device the bridge added, and forget what they held.</summary>
        private static void RemoveAriaDevices()
        {
            _input.Clear();
            _heldKeys.Clear();
            _buttons = 0;
            _pointer = Vector2.zero;
            _ariaKeyboard = null;
            _ariaMouse = null;

            List<InputDevice> doomed = new List<InputDevice>();
            foreach (InputDevice device in InputSystem.devices)
            {
                if (device != null && device.name != null &&
                    device.name.StartsWith(AriaDevicePrefix, StringComparison.Ordinal))
                {
                    doomed.Add(device);
                }
            }

            foreach (InputDevice device in doomed) InputSystem.RemoveDevice(device);
        }
#endif

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
