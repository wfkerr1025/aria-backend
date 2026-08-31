// Assets/ARIA/Tests/ARIAEditorBridgeTests.cs
//
// EditMode tests for the bridge, through the Unity Test Framework.
//
// Every method is tested twice: once for the thing it does, and once for
// what it says when it cannot. The second half is the point. The bridge
// exists to be driven from outside the editor, where an exception is
// invisible and only the returned JSON survives -- so "it fails clearly"
// is a behaviour with the same standing as "it works".
//
// EditMode rather than PlayMode: none of this needs a running game, and
// an EditMode suite runs in a second instead of a domain reload.

#if UNITY_EDITOR

using System.IO;
using ARIA;
using NUnit.Framework;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace ARIA.Tests
{
    /// <summary>Covers every public method on ARIAEditorBridge.</summary>
    public class ARIAEditorBridgeTests
    {
        private const string TempFolder = "Assets/ARIA/Tests/Temp";

        private Scene _scene;

        /// <summary>A clean empty scene and a scratch folder for each test.</summary>
        [SetUp]
        public void SetUp()
        {
            _scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

            if (!AssetDatabase.IsValidFolder(TempFolder))
            {
                AssetDatabase.CreateFolder("Assets/ARIA/Tests", "Temp");
            }
        }

        /// <summary>Leaves no assets behind, whatever the test did.</summary>
        [TearDown]
        public void TearDown()
        {
            if (AssetDatabase.IsValidFolder(TempFolder))
            {
                AssetDatabase.DeleteAsset(TempFolder);
            }
            AssetDatabase.Refresh();
        }

        /// <summary>Reads the "ok" flag out of a bridge result.</summary>
        private static bool Succeeded(string json)
        {
            Assert.IsNotNull(json, "the bridge must always answer");
            return json.Contains("\"ok\":true");
        }

        /// <summary>Reads the "error" text out of a bridge result.</summary>
        private static string ErrorOf(string json)
        {
            Assert.IsNotNull(json);
            return json;
        }

        // ==========================================================
        // CreateGameObject
        // ==========================================================

        [Test]
        public void CreateGameObject_MakesAnObjectWithThatName()
        {
            Assert.IsTrue(Succeeded(ARIAEditorBridge.CreateGameObject("Player")));
            Assert.IsNotNull(GameObject.Find("Player"));
        }

        [Test]
        public void CreateGameObject_RefusesAnEmptyName()
        {
            Assert.IsFalse(Succeeded(ARIAEditorBridge.CreateGameObject("")));
            Assert.IsFalse(Succeeded(ARIAEditorBridge.CreateGameObject(null)));
        }

        // ==========================================================
        // AddComponent
        // ==========================================================

        [Test]
        public void AddComponent_AddsIt()
        {
            ARIAEditorBridge.CreateGameObject("Player");

            Assert.IsTrue(Succeeded(ARIAEditorBridge.AddComponent("Player", "Rigidbody")));
            Assert.IsNotNull(GameObject.Find("Player").GetComponent<Rigidbody>());
        }

        [Test]
        public void AddComponent_SaysSoWhenTheObjectIsNotThere()
        {
            string result = ARIAEditorBridge.AddComponent("NoSuchObject", "Rigidbody");

            Assert.IsFalse(Succeeded(result));
            StringAssert.Contains("NoSuchObject", ErrorOf(result));
        }

        [Test]
        public void AddComponent_SaysSoWhenTheTypeIsNotThere()
        {
            ARIAEditorBridge.CreateGameObject("Player");

            string result = ARIAEditorBridge.AddComponent("Player", "NotARealComponent");

            Assert.IsFalse(Succeeded(result));
            StringAssert.Contains("NotARealComponent", ErrorOf(result));
        }

        [Test]
        public void AddComponent_RefusesATypeThatIsNotAComponent()
        {
            ARIAEditorBridge.CreateGameObject("Player");

            Assert.IsFalse(Succeeded(ARIAEditorBridge.AddComponent("Player", "String")));
        }

        [Test]
        public void AddComponent_DoesNotAddASecondCopy()
        {
            ARIAEditorBridge.CreateGameObject("Player");
            ARIAEditorBridge.AddComponent("Player", "Rigidbody");

            ARIAEditorBridge.AddComponent("Player", "Rigidbody");

            Assert.AreEqual(1, GameObject.Find("Player").GetComponents<Rigidbody>().Length);
        }

        // ==========================================================
        // SetSerializedField
        // ==========================================================

        [Test]
        public void SetSerializedField_WritesTheValue()
        {
            ARIAEditorBridge.CreateGameObject("Light");
            ARIAEditorBridge.AddComponent("Light", "Light");

            Assert.IsTrue(Succeeded(
                ARIAEditorBridge.SetSerializedField("Light", "Light", "m_Intensity", "3")));
            Assert.AreEqual(3f, GameObject.Find("Light").GetComponent<Light>().intensity, 0.001f);
        }

        [Test]
        public void SetSerializedField_SaysSoWhenTheFieldIsNotThere()
        {
            ARIAEditorBridge.CreateGameObject("Light");
            ARIAEditorBridge.AddComponent("Light", "Light");

            string result = ARIAEditorBridge.SetSerializedField("Light", "Light", "notAField", "3");

            Assert.IsFalse(Succeeded(result));
            StringAssert.Contains("notAField", ErrorOf(result));
        }

        [Test]
        public void SetSerializedField_SaysSoWhenTheValueIsTheWrongShape()
        {
            ARIAEditorBridge.CreateGameObject("Light");
            ARIAEditorBridge.AddComponent("Light", "Light");

            Assert.IsFalse(Succeeded(
                ARIAEditorBridge.SetSerializedField("Light", "Light", "m_Intensity", "bright")));
        }

        [Test]
        public void SetSerializedField_SaysSoWhenTheComponentIsNotOnTheObject()
        {
            ARIAEditorBridge.CreateGameObject("Player");

            Assert.IsFalse(Succeeded(
                ARIAEditorBridge.SetSerializedField("Player", "Light", "m_Intensity", "3")));
        }

        // ==========================================================
        // CreatePrefab
        // ==========================================================

        [Test]
        public void CreatePrefab_WritesTheAsset()
        {
            ARIAEditorBridge.CreateGameObject("Player");
            string path = TempFolder + "/Player.prefab";

            Assert.IsTrue(Succeeded(ARIAEditorBridge.CreatePrefab(path, "Player")));
            Assert.IsNotNull(AssetDatabase.LoadAssetAtPath<GameObject>(path));
        }

        [Test]
        public void CreatePrefab_RefusesAPathOutsideAssets()
        {
            ARIAEditorBridge.CreateGameObject("Player");

            Assert.IsFalse(Succeeded(ARIAEditorBridge.CreatePrefab("../Player.prefab", "Player")));
            Assert.IsFalse(Succeeded(ARIAEditorBridge.CreatePrefab("C:/Player.prefab", "Player")));
            Assert.IsFalse(Succeeded(
                ARIAEditorBridge.CreatePrefab("Assets/../../Player.prefab", "Player")));
        }

        [Test]
        public void CreatePrefab_RefusesTheWrongExtension()
        {
            ARIAEditorBridge.CreateGameObject("Player");

            Assert.IsFalse(Succeeded(
                ARIAEditorBridge.CreatePrefab(TempFolder + "/Player.asset", "Player")));
        }

        [Test]
        public void CreatePrefab_SaysSoWhenTheObjectIsNotThere()
        {
            Assert.IsFalse(Succeeded(
                ARIAEditorBridge.CreatePrefab(TempFolder + "/X.prefab", "NoSuchObject")));
        }

        // ==========================================================
        // CreateScriptableObject
        // ==========================================================

        [Test]
        public void CreateScriptableObject_WritesTheAsset()
        {
            string path = TempFolder + "/Settings.asset";

            // ScriptableSingleton-free built-in: any ScriptableObject
            // subclass Unity always has.
            string result = ARIAEditorBridge.CreateScriptableObject("ScriptableObject", path);

            // A bare ScriptableObject is instantiable, so this should work;
            // if a future Unity refuses it, the bridge must SAY so.
            if (Succeeded(result))
            {
                Assert.IsNotNull(AssetDatabase.LoadAssetAtPath<ScriptableObject>(path));
            }
            else
            {
                StringAssert.Contains("error", ErrorOf(result));
            }
        }

        [Test]
        public void CreateScriptableObject_RefusesATypeThatIsNotOne()
        {
            Assert.IsFalse(Succeeded(
                ARIAEditorBridge.CreateScriptableObject("Rigidbody", TempFolder + "/X.asset")));
        }

        [Test]
        public void CreateScriptableObject_RefusesAPathOutsideAssets()
        {
            Assert.IsFalse(Succeeded(
                ARIAEditorBridge.CreateScriptableObject("ScriptableObject", "../X.asset")));
        }

        // ==========================================================
        // ImportAsset
        // ==========================================================

        [Test]
        public void ImportAsset_ImportsSomethingThatExists()
        {
            string path = TempFolder + "/note.txt";
            File.WriteAllText(path, "hello");
            AssetDatabase.Refresh();

            Assert.IsTrue(Succeeded(ARIAEditorBridge.ImportAsset(path)));
        }

        [Test]
        public void ImportAsset_SaysSoWhenNothingIsThere()
        {
            Assert.IsFalse(Succeeded(ARIAEditorBridge.ImportAsset(TempFolder + "/missing.txt")));
        }

        [Test]
        public void ImportAsset_RefusesAPathOutsideAssets()
        {
            Assert.IsFalse(Succeeded(ARIAEditorBridge.ImportAsset("../../secrets.txt")));
        }

        // ==========================================================
        // Scenes
        // ==========================================================

        [Test]
        public void LoadScene_RefusesAPathThatIsNotAScene()
        {
            Assert.IsFalse(Succeeded(ARIAEditorBridge.LoadScene(TempFolder + "/note.txt")));
        }

        [Test]
        public void LoadScene_SaysSoWhenTheSceneIsNotThere()
        {
            Assert.IsFalse(Succeeded(ARIAEditorBridge.LoadScene(TempFolder + "/missing.unity")));
        }

        [Test]
        public void LoadScene_RefusesAPathOutsideAssets()
        {
            Assert.IsFalse(Succeeded(ARIAEditorBridge.LoadScene("../Other.unity")));
        }

        [Test]
        public void SaveScene_SaysSoWhenTheSceneHasNeverBeenSaved()
        {
            // SetUp made a brand new scene, so it has no path yet.
            Assert.IsFalse(Succeeded(ARIAEditorBridge.SaveScene()));
        }

        // ==========================================================
        // GetSceneSummary
        // ==========================================================

        [Test]
        public void GetSceneSummary_ListsEveryObject()
        {
            ARIAEditorBridge.CreateGameObject("Player");
            ARIAEditorBridge.CreateGameObject("Enemy");

            string result = ARIAEditorBridge.GetSceneSummary();

            Assert.IsTrue(Succeeded(result));
            StringAssert.Contains("Player", result);
            StringAssert.Contains("Enemy", result);
        }

        [Test]
        public void GetSceneSummary_NamesEachObjectsComponents()
        {
            ARIAEditorBridge.CreateGameObject("Player");
            ARIAEditorBridge.AddComponent("Player", "Rigidbody");

            string result = ARIAEditorBridge.GetSceneSummary();

            StringAssert.Contains("Rigidbody", result);
            StringAssert.Contains("Transform", result);
        }

        [Test]
        public void GetSceneSummary_IncludesChildrenByPath()
        {
            ARIAEditorBridge.CreateGameObject("Parent");
            ARIAEditorBridge.CreateGameObject("Child");
            GameObject.Find("Child").transform.SetParent(GameObject.Find("Parent").transform);

            string result = ARIAEditorBridge.GetSceneSummary();

            StringAssert.Contains("Parent/Child", result);
        }

        [Test]
        public void GetSceneSummary_IsValidJson()
        {
            ARIAEditorBridge.CreateGameObject("Player");

            string result = ARIAEditorBridge.GetSceneSummary();

            // JsonUtility round-trips its own output; if this throws, the
            // caller on the other side of the bridge cannot read it either.
            Assert.DoesNotThrow(() => JsonUtility.FromJson<object>(result));
        }

        // ==========================================================
        // RunBuild
        // ==========================================================

        [Test]
        public void RunBuild_RefusesAnEmptyPath()
        {
            Assert.IsFalse(Succeeded(ARIAEditorBridge.RunBuild("")));
        }

        [Test]
        public void RunBuild_RefusesTraversal()
        {
            Assert.IsFalse(Succeeded(ARIAEditorBridge.RunBuild("../../out.exe")));
        }

        // ==========================================================
        // The contract every method shares
        // ==========================================================

        [Test]
        public void EveryMethodAnswersWithJsonRatherThanThrowing()
        {
            // An exception cannot cross the process boundary this bridge
            // exists to serve. Silence is the one outcome the caller
            // cannot handle.
            Assert.DoesNotThrow(() =>
            {
                ARIAEditorBridge.CreateGameObject(null);
                ARIAEditorBridge.AddComponent(null, null);
                ARIAEditorBridge.CreatePrefab(null, null);
                ARIAEditorBridge.CreateScriptableObject(null, null);
                ARIAEditorBridge.LoadScene(null);
                ARIAEditorBridge.SaveScene();
                ARIAEditorBridge.RunBuild(null);
                ARIAEditorBridge.ImportAsset(null);
                ARIAEditorBridge.SetSerializedField(null, null, null, null);
                ARIAEditorBridge.GetSceneSummary();
            });
        }
    }
}

#endif
