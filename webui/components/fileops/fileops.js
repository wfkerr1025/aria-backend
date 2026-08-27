import { Bridge } from "../../core/bridge.js";

function fileOpsLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("FileOps", msg);
    }
  } catch (err) {
    console.error("[FileOps LOG ERROR]", err);
  }
}

fileOpsLog("=== FILEOPS MODULE LOADED ===");

export const FileOps = {
  selected: null,

  init() {
    fileOpsLog("FileOps.init() called.");

    this.cache();
    this.bindUI();
    this.bindBridge();

    fileOpsLog("Requesting initial directory listing.");
    Bridge.send({ type: "fileops_request" });

    fileOpsLog("FileOps subsystem ready.");
  },

  cache() {
    fileOpsLog("Caching DOM elements.");

    this.directory = document.getElementById("fileops-directory");
    this.preview = document.getElementById("fileops-preview");

    this.refreshBtn = document.getElementById("fileops-refresh-btn");
    this.openBtn = document.getElementById("fileops-open-btn");
    this.deleteBtn = document.getElementById("fileops-delete-btn");
    this.saveBtn = document.getElementById("fileops-save-btn");

    fileOpsLog(
      "CACHE RESULT: " +
        JSON.stringify({
          directory: !!this.directory,
          preview: !!this.preview,
          refreshBtn: !!this.refreshBtn,
          openBtn: !!this.openBtn,
          deleteBtn: !!this.deleteBtn,
          saveBtn: !!this.saveBtn
        })
    );

    if (!this.directory || !this.preview) {
      fileOpsLog("ERROR: Missing required FileOps DOM elements.");
    }
  },

  bindUI() {
    fileOpsLog("Binding UI events.");

    this.refreshBtn.addEventListener("click", () => {
      fileOpsLog("Refresh clicked → fileops_request");
      Bridge.send({ type: "fileops_request" });
    });

    this.openBtn.addEventListener("click", () => {
      if (!this.selected) {
        fileOpsLog("Open clicked but no file selected.");
        return;
      }
      fileOpsLog("Open clicked → " + this.selected);
      Bridge.send({ type: "fileops_open", path: this.selected });
    });

    this.deleteBtn.addEventListener("click", () => {
      if (!this.selected) {
        fileOpsLog("Delete clicked but no file selected.");
        return;
      }
      fileOpsLog("Delete clicked → " + this.selected);
      Bridge.send({ type: "fileops_delete", path: this.selected });
    });

    this.saveBtn.addEventListener("click", () => {
      if (!this.selected) {
        fileOpsLog("Save clicked but no file selected.");
        return;
      }
      fileOpsLog("Save clicked → " + this.selected);
      Bridge.send({
        type: "fileops_save",
        path: this.selected,
        content: this.preview.textContent
      });
    });

    fileOpsLog("UI bound successfully.");
  },

  bindBridge() {
    fileOpsLog("Binding Bridge handlers.");

    Bridge.on("fileops_update", (packet) => {
      fileOpsLog("Received fileops_update with " + packet.files.length + " files.");
      this.renderDirectory(packet.files);
    });

    Bridge.on("fileops_preview", (packet) => {
      fileOpsLog("Received fileops_preview for file.");
      this.renderPreview(packet.content);
    });

    Bridge.on("fileops_deleted", () => {
      fileOpsLog("Received fileops_deleted → clearing preview + refreshing directory.");
      this.preview.textContent = "";
      this.selected = null;
      Bridge.send({ type: "fileops_request" });
    });

    Bridge.on("fileops_saved", () => {
      fileOpsLog("Received fileops_saved confirmation.");
    });

    fileOpsLog("Bridge handlers registered.");
  },

  renderDirectory(files) {
    fileOpsLog("Rendering directory listing (" + files.length + " files).");

    this.directory.innerHTML = "";
    this.selected = null;

    files.forEach(file => {
      const item = document.createElement("div");
      item.className = "fileops-item";
      item.textContent = file;

      item.addEventListener("click", () => {
        fileOpsLog("File selected → " + file);
        this.selectFile(file, item);
      });

      this.directory.appendChild(item);
    });

    fileOpsLog("Directory render complete.");
  },

  selectFile(file, element) {
    fileOpsLog("selectFile() → " + file);

    this.selected = file;

    [...this.directory.children].forEach(child =>
      child.classList.remove("active")
    );

    element.classList.add("active");

    fileOpsLog("Requesting preview for " + file);
    Bridge.send({ type: "fileops_open", path: file });
  },

  renderPreview(content) {
    fileOpsLog("Rendering preview. Length=" + (content?.length || 0));
    this.preview.textContent = content || "(Empty file)";
  }
};

fileOpsLog("FileOps exported.");
