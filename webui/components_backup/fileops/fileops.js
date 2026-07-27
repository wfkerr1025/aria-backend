import { Bridge } from "../../core/bridge.js";

export const FileOps = {
  selected: null,

  init() {
    this.cache();
    this.bindUI();
    this.bindBridge();

    // Request initial directory listing
    Bridge.send({ type: "fileops_request" });
  },

  cache() {
    this.directory = document.getElementById("fileops-directory");
    this.preview = document.getElementById("fileops-preview");

    this.refreshBtn = document.getElementById("fileops-refresh-btn");
    this.openBtn = document.getElementById("fileops-open-btn");
    this.deleteBtn = document.getElementById("fileops-delete-btn");
    this.saveBtn = document.getElementById("fileops-save-btn");
  },

  bindUI() {
    this.refreshBtn.addEventListener("click", () => {
      Bridge.send({ type: "fileops_request" });
    });

    this.openBtn.addEventListener("click", () => {
      if (!this.selected) return;
      Bridge.send({ type: "fileops_open", path: this.selected });
    });

    this.deleteBtn.addEventListener("click", () => {
      if (!this.selected) return;
      Bridge.send({ type: "fileops_delete", path: this.selected });
    });

    this.saveBtn.addEventListener("click", () => {
      if (!this.selected) return;
      Bridge.send({
        type: "fileops_save",
        path: this.selected,
        content: this.preview.textContent
      });
    });
  },

  bindBridge() {
    Bridge.on("fileops_update", (packet) => {
      this.renderDirectory(packet.files);
    });

    Bridge.on("fileops_preview", (packet) => {
      this.renderPreview(packet.content);
    });

    Bridge.on("fileops_deleted", () => {
      this.preview.textContent = "";
      this.selected = null;
      Bridge.send({ type: "fileops_request" });
    });

    Bridge.on("fileops_saved", () => {
      // Optional: add a small visual confirmation
    });
  },

  renderDirectory(files) {
    this.directory.innerHTML = "";
    this.selected = null;

    files.forEach(file => {
      const item = document.createElement("div");
      item.className = "fileops-item";
      item.textContent = file;

      item.addEventListener("click", () => {
        this.selectFile(file, item);
      });

      this.directory.appendChild(item);
    });
  },

  selectFile(file, element) {
    this.selected = file;

    // Clear active state
    [...this.directory.children].forEach(child =>
      child.classList.remove("active")
    );

    element.classList.add("active");

    Bridge.send({ type: "fileops_open", path: file });
  },

  renderPreview(content) {
    this.preview.textContent = content || "(Empty file)";
  }
};
