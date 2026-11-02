const bridge = window.AstrBotPluginView;
const DAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

const el = (id) => document.getElementById(id);

// AstrBot 的前端会把插件返回值拆一层，不同版本拆的层数不一样，
// 所以这里一路拆到看见 courses 数组为止，避免"读取失败"。
function unwrap(res) {
  let cur = res;
  for (let depth = 0; depth < 4; depth += 1) {
    if (!cur || typeof cur !== "object") break;
    if (Array.isArray(cur.courses)) return cur;
    if (cur.data && typeof cur.data === "object") {
      cur = cur.data;
      continue;
    }
    break;
  }
  return cur;
}
let state = null;
let editing = null;   // {kind: "course"|"block", id: string|null}

// ---------------------------------------------------------------- 数据

async function load() {
  await bridge.ready();
  const res = await bridge.apiGet("data");
  const data = unwrap(res);
  if (!data || !Array.isArray(data.courses)) {
    throw new Error(`返回结构异常：${JSON.stringify(res).slice(0, 200)}`);
  }
  state = data;
  el("meta").textContent =
    `课程 ${state.courses.length} 节 · 自习块 ${state.blocks.length} 个 · ` +
    `三餐 ${state.meals.length} 顿`;
  renderAll();
}

async function commit(message) {
  try {
    const res = await bridge.apiPost("save", { data: state });
    const saved = unwrap(res);
    if (!saved || !Array.isArray(saved.courses)) {
      throw new Error(res?.error || `保存返回异常：${JSON.stringify(res).slice(0, 200)}`);
    }
    state = saved;
    renderAll();
    status(message || "已保存");
  } catch (err) {
    status(String(err.message || err), true);
  }
}

let statusTimer = null;
function status(text, isError = false) {
  const node = el("status");
  node.textContent = text;
  node.classList.toggle("error", isError);
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => {
    node.textContent = "";
    node.classList.remove("error");
  }, 4000);
}

// ---------------------------------------------------------------- 周次

function fmtWeeks(list) {
  return (list || [])
    .map(([a, b]) => (a === b ? `${a}` : `${a}-${b}`))
    .join(", ");
}

function parseWeeks(text) {
  const out = [];
  for (const part of String(text || "").split(/[,，;；\s]+/).filter(Boolean)) {
    const range = part.match(/^(\d+)\s*[-~—]\s*(\d+)$/);
    if (range) {
      out.push([Number(range[1]), Number(range[2])]);
    } else if (/^\d+$/.test(part)) {
      out.push([Number(part), Number(part)]);
    } else {
      throw new Error(`周次格式看不懂：${part}`);
    }
  }
  return out;
}

// ---------------------------------------------------------------- 渲染

function renderAll() {
  renderBoard();
  renderBlocks();
  renderMeals();
  renderSleep();
  renderSettings();
}

function itemCard(kind, item) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = `card ${kind}`;
  card.innerHTML = `
    <span class="time">${item.start}</span>
    <strong>${escapeHtml(item.name || item.label)}</strong>
    <span class="sub">${escapeHtml(item.room || item.reason || "")}</span>
    <span class="weeks">${
      kind === "block"
        ? (fmtWeeks(item.skip_weeks) ? `跳过 ${fmtWeeks(item.skip_weeks)}` : "每周")
        : fmtWeeks(item.weeks)
    }</span>`;
  card.addEventListener("click", () => openEditor(kind, item));
  return card;
}

function renderBoard() {
  const board = el("board");
  board.innerHTML = "";
  for (let day = 0; day < 7; day += 1) {
    const column = document.createElement("div");
    column.className = "column";
    const courses = state.courses.filter((c) => c.day === day);
    const blocks = state.blocks.filter((b) => b.day === day);
    const rows = [
      ...courses.map((item) => ({ kind: "course", item })),
      ...blocks.map((item) => ({ kind: "block", item })),
    ].sort((a, b) => a.item.start.localeCompare(b.item.start));

    column.innerHTML = `<h3>${DAYS[day]}<span>${rows.length}</span></h3>`;
    for (const row of rows) column.appendChild(itemCard(row.kind, row.item));

    const add = document.createElement("button");
    add.type = "button";
    add.className = "add";
    add.textContent = "＋ 新增";
    add.addEventListener("click", () => openEditor("course", null, day));
    column.appendChild(add);
    board.appendChild(column);
  }
}

function renderBlocks() {
  const box = el("blockList");
  box.innerHTML = "";
  const rows = [...state.blocks].sort(
    (a, b) => a.day - b.day || a.start.localeCompare(b.start),
  );
  for (const block of rows) {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `
      <span>${DAYS[block.day]}</span>
      <span>${block.start}</span>
      <strong>${escapeHtml(block.label)}</strong>
      <span class="dim">${escapeHtml(block.reason || "")}</span>
      <span class="dim">不提醒周次：${fmtWeeks(block.skip_weeks) || "无"}</span>`;
    row.appendChild(button("编辑", () => openEditor("block", block)));
    row.appendChild(button("删除", () => removeItem("block", block.id), "danger"));
    box.appendChild(row);
  }
}

function renderMeals() {
  const box = el("mealList");
  box.innerHTML = "";
  for (const meal of state.meals) {
    const row = document.createElement("div");
    row.className = "row";
    const time = input("time", meal.at);
    const label = input("text", meal.label);
    const hint = input("text", meal.hint || "");
    time.addEventListener("change", () => { meal.at = time.value; commit(); });
    label.addEventListener("change", () => { meal.label = label.value; commit(); });
    hint.addEventListener("change", () => { meal.hint = hint.value; commit(); });
    row.append(time, label, hint, button("删除", () => removeItem("meal", meal.id), "danger"));
    box.appendChild(row);
  }
}

function renderSleep() {
  const box = el("sleepList");
  box.innerHTML = "";
  for (const phase of state.sleep_plan) {
    const row = document.createElement("div");
    row.className = "row";
    const from = input("number", phase.from_week, { min: 1 });
    const to = input("number", phase.to_week, { min: 1 });
    const bed = input("time", phase.bedtime);
    const wakeClass = input("time", phase.wake_class);
    const wakeLate = input("time", phase.wake_late);
    const nap = input("number", phase.nap, { min: 0, max: 120 });
    const bind = (node, key, cast = (v) => v) => {
      node.addEventListener("change", () => {
        phase[key] = cast(node.value);
        commit();
      });
    };
    bind(from, "from_week", Number);
    bind(to, "to_week", Number);
    bind(bed, "bedtime");
    bind(wakeClass, "wake_class");
    bind(wakeLate, "wake_late");
    bind(nap, "nap", Number);
    row.append(
      span("周"), from, span("→"), to, bed, wakeClass, wakeLate,
      span("午休"), nap, button("删除", () => removeItem("sleep_plan", phase.id), "danger"),
    );
    box.appendChild(row);
  }
}

function renderSettings() {
  const s = state.settings;
  el("ownerQq").value = s.owner_qq || "";
  el("semesterStart").value = s.semester_start;
  el("sleepStart").value = s.sleep_start;
  el("lightoutAt").value = s.lightout_at;
  el("timezone").value = s.timezone || "Asia/Shanghai";
}

// ---------------------------------------------------------------- 编辑弹窗

function openEditor(kind, item, day = 0) {
  editing = { kind, id: item?.id || null };
  el("editorTitle").textContent = item ? "编辑" : "新增";
  el("fKind").value = kind;
  el("fDay").value = String(item?.day ?? day);
  el("fStart").value = item?.start || "08:00";
  el("fSpan").value = item?.span || "";
  el("fName").value = item?.name || item?.label || "";
  el("fExtra").value = item?.room || item?.reason || "";
  el("fWeeks").value = item
    ? fmtWeeks(item.weeks || item.skip_weeks)
    : "1-16";
  el("fDelete").hidden = !item;
  el("editor").showModal();
}

function readEditor() {
  const kind = el("fKind").value;
  const start = el("fStart").value;
  if (!start) throw new Error("先填开始时间");
  const name = el("fName").value.trim();
  if (!name) throw new Error("先填名称");
  const weeks = parseWeeks(el("fWeeks").value);
  const base = {
    day: Number(el("fDay").value),
    start,
    span: el("fSpan").value.trim() || start,
  };
  if (kind === "course") {
    return { ...base, name, room: el("fExtra").value.trim(), weeks };
  }
  return { ...base, label: name, reason: el("fExtra").value.trim(), skip_weeks: weeks };
}

async function saveEditor() {
  try {
    const item = readEditor();
    const kind = el("fKind").value;
    const table = kind === "course" ? "courses" : "blocks";
    if (editing.id) {
      const index = state[table].findIndex((row) => row.id === editing.id);
      if (index >= 0) state[table][index] = { ...state[table][index], ...item };
      else state[table].push(item);
    } else {
      state[table].push(item);
    }
    el("editor").close();
    await commit("已保存这一条");
  } catch (err) {
    status(String(err.message || err), true);
  }
}

async function removeItem(table, id) {
  state[table] = state[table].filter((row) => row.id !== id);
  await commit("已删除");
}

// ---------------------------------------------------------------- 小工具

function escapeHtml(text) {
  return String(text || "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function button(label, onClick, className = "ghost") {
  const node = document.createElement("button");
  node.type = "button";
  node.className = className;
  node.textContent = label;
  node.addEventListener("click", onClick);
  return node;
}

function input(type, value, attrs = {}) {
  const node = document.createElement("input");
  node.type = type;
  node.value = value ?? "";
  for (const [key, val] of Object.entries(attrs)) node.setAttribute(key, String(val));
  return node;
}

function span(text) {
  const node = document.createElement("span");
  node.textContent = text;
  node.className = "dim";
  return node;
}

// ---------------------------------------------------------------- 事件

el("tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-tab]");
  if (!tab) return;
  for (const node of el("tabs").querySelectorAll("button")) {
    node.classList.toggle("active", node === tab);
  }
  for (const name of ["courses", "blocks", "meals", "sleep", "settings"]) {
    el(`tab-${name}`).hidden = name !== tab.dataset.tab;
  }
});

el("reload").addEventListener("click", () => {
  load().then(() => status("已重新读取")).catch((err) => status(String(err), true));
});

el("fSave").addEventListener("click", saveEditor);
el("fCancel").addEventListener("click", () => el("editor").close());
el("fDelete").addEventListener("click", async () => {
  if (!editing?.id) return;
  const table = el("fKind").value === "course" ? "courses" : "blocks";
  el("editor").close();
  await removeItem(table, editing.id);
});

el("ownerQq").addEventListener("change", (e) => {
  state.settings.owner_qq = e.target.value.trim();
  commit("主人 QQ 已绑定");
});
el("semesterStart").addEventListener("change", (e) => {
  state.settings.semester_start = e.target.value;
  commit();
});
el("sleepStart").addEventListener("change", (e) => {
  state.settings.sleep_start = e.target.value;
  commit();
});
el("lightoutAt").addEventListener("change", (e) => {
  state.settings.lightout_at = e.target.value;
  commit();
});
el("timezone").addEventListener("change", (e) => {
  state.settings.timezone = e.target.value.trim() || "Asia/Shanghai";
  commit();
});

for (const node of document.querySelectorAll("[data-add]")) {
  node.addEventListener("click", () => {
    const kind = node.dataset.add;
    if (kind === "meal") {
      state.meals.push({ at: "12:00", label: "加餐", hint: "" });
      commit("已新增，请改时间");
    } else if (kind === "sleep") {
      const last = state.sleep_plan.at(-1);
      state.sleep_plan.push({
        from_week: (last?.to_week || 1) + 1,
        to_week: (last?.to_week || 1) + 1,
        bedtime: last?.bedtime || "00:00",
        wake_class: last?.wake_class || "07:15",
        wake_late: last?.wake_late || "07:30",
        nap: last?.nap || 25,
      });
      commit("已新增，请改周次");
    } else {
      openEditor("block", null);
    }
  });
}

load()
  .then(() => status("已就绪"))
  .catch((err) => status(`加载失败：${err.message || err}`, true));
