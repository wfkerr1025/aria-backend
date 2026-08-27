// components/module_row/module_row.js
//
// Two deliberately separate row builders for the Settings panel's
// "Module Keys" list (webui/pages/modules/modules.js):
//
//   - RealModuleRow(module, { onSave, onDelete }) — one row per REAL
//     backend module (from modules_list_result), whether it's a
//     user-added module or a self-discovered one (backend.core.
//     module_manager.KNOWN_MODULES, materialized into the key store by
//     key_manager.ensure_module_entry() — see that function's docstring
//     for why a self-discovered module is a real store entry, not a
//     computed placeholder). Both kinds render identically: name,
//     status ("Key set" / "Not configured"), an editable API-key input,
//     a Save button, and a Remove button — a self-discovered module
//     that hasn't been configured yet is directly configurable right
//     here, not only through AddNewModuleRow's generic form.
//
//   - AddNewModuleRow({ onAdd })            — the "Add New Module" form,
//     for module names that aren't in the list at all yet. This is NOT
//     a module and must never be mistaken for one: it has no module
//     name, no status, no Remove button, and none of RealModuleRow's
//     CSS classes. Previously this form's inputs lived inline in
//     settings.html and were wired up by hand in settings.js; pulling
//     it out here (as its own component, never appended to the same
//     list RealModuleRow rows go into) is what guarantees the two can
//     never visually or structurally collide into a phantom
//     "Weather — Not configured" placeholder row.
//
// Both are pure DOM-node builders (no module-level state), matching the
// convention already used by webui/pages/models/models.js's buildCard()/
// buildKeyCard() — the caller (modules.js) owns fetching data and
// wiring the result into the page.

export function RealModuleRow(module, { onSave, onDelete }) {
  const row = document.createElement("div");
  row.className = "module-row";

  const label = document.createElement("label");
  label.textContent = module.name;
  label.className = "module-row-name" + (module.configured ? " configured" : "");

  const status = document.createElement("span");
  status.className = "module-row-status";
  status.textContent = module.configured ? "Key set" : "Not configured";

  const keyInput = document.createElement("input");
  keyInput.type = "password";
  keyInput.className = "module-row-key-input";
  keyInput.placeholder = module.configured ? "•••••••• (key set)" : "Enter API key";

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn btn-secondary module-row-save-btn";
  saveBtn.textContent = "Save";
  saveBtn.addEventListener("click", () => {
    const value = keyInput.value.trim();
    if (!value) return;
    onSave(module.name, value);
    keyInput.value = "";
  });

  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.className = "btn btn-secondary module-row-remove-btn";
  removeBtn.textContent = "Remove";
  removeBtn.disabled = !module.configured;
  removeBtn.addEventListener("click", () => onDelete(module.name));

  row.appendChild(label);
  row.appendChild(status);
  row.appendChild(keyInput);
  row.appendChild(saveBtn);
  row.appendChild(removeBtn);
  return row;
}

export function AddNewModuleRow({ onAdd }) {
  const container = document.createElement("div");
  container.className = "add-module-row";

  const header = document.createElement("h3");
  header.className = "add-module-row-header";
  header.textContent = "Add New Module";

  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.id = "new-module-name";
  nameInput.className = "add-module-row-input";
  nameInput.placeholder = "Module name (e.g. weather)";

  const keyInput = document.createElement("input");
  keyInput.type = "password";
  keyInput.id = "new-module-key";
  keyInput.className = "add-module-row-input";
  keyInput.placeholder = "API key";

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.id = "add-module-key-btn";
  addBtn.className = "btn btn-secondary add-module-row-btn";
  addBtn.textContent = "Add";
  addBtn.addEventListener("click", () => {
    const name = nameInput.value.trim();
    const key = keyInput.value.trim();
    if (!name || !key) return;
    onAdd(name, key);
    nameInput.value = "";
    keyInput.value = "";
  });

  container.appendChild(header);
  container.appendChild(nameInput);
  container.appendChild(keyInput);
  container.appendChild(addBtn);
  return container;
}
