const sourceTabs = document.querySelectorAll(".source-tab");
const fileSource = document.querySelector("#file-source");
const feishuSource = document.querySelector("#feishu-source");
const form = document.querySelector("#job-form");
const fileInput = document.querySelector("#prd-file");
const fileLabel = document.querySelector("#file-label");
const dropZone = document.querySelector("#drop-zone");
const formError = document.querySelector("#form-error");
const startButton = document.querySelector("#start-button");
const activeJob = document.querySelector("#active-job");
const jobList = document.querySelector("#job-list");
let sourceMode = "file";
let pollTimer = null;

for (const tab of sourceTabs) {
  tab.addEventListener("click", () => {
    sourceMode = tab.dataset.source;
    for (const item of sourceTabs) item.classList.toggle("is-active", item === tab);
    fileSource.classList.toggle("is-active", sourceMode === "file");
    feishuSource.classList.toggle("is-active", sourceMode === "feishu");
    formError.textContent = "";
  });
}

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (file) fileLabel.textContent = file.name;
});

for (const eventName of ["dragenter", "dragover"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("is-dragover");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("is-dragover");
  });
}
dropZone.addEventListener("drop", (event) => {
  const file = event.dataTransfer.files[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  fileLabel.textContent = file.name;
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  formError.textContent = "";
  startButton.disabled = true;
  startButton.querySelector("span").textContent = "Starting";
  try {
    let response;
    const title = document.querySelector("#project-title").value;
    if (sourceMode === "feishu") {
      response = await fetch("/api/jobs/from-feishu", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: document.querySelector("#feishu-url").value, title }),
      });
    } else {
      const file = fileInput.files[0];
      if (!file) throw new Error("请先选择一个 PRD 文件");
      const payload = new FormData();
      payload.append("file", file);
      payload.append("title", title);
      response = await fetch("/api/jobs", { method: "POST", body: payload });
    }
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "任务创建失败");
    showActiveJob(body);
    poll(body.job_id);
    loadJobs();
  } catch (error) {
    formError.textContent = error.message;
  } finally {
    startButton.disabled = false;
    startButton.querySelector("span").textContent = "Start";
  }
});

document.querySelector("#refresh-button").addEventListener("click", loadJobs);

async function poll(jobId) {
  if (pollTimer) clearInterval(pollTimer);
  const refresh = async () => {
    const response = await fetch(`/api/jobs/${jobId}`);
    if (!response.ok) return;
    const job = await response.json();
    showActiveJob(job);
    if (["COMPLETED", "FAILED"].includes(job.status)) {
      clearInterval(pollTimer);
      pollTimer = null;
      loadJobs();
    }
  };
  await refresh();
  pollTimer = setInterval(refresh, 1200);
}

async function loadJobs() {
  const response = await fetch("/api/jobs");
  if (!response.ok) return;
  const body = await response.json();
  jobList.innerHTML = body.items.length ? body.items.map(renderJob).join("") : '<div class="empty-state">还没有运行记录</div>';
  for (const row of jobList.querySelectorAll("[data-job-id]")) {
    row.addEventListener("click", () => fetchJob(row.dataset.jobId));
  }
}

async function fetchJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  if (response.ok) showActiveJob(await response.json());
}

function showActiveJob(job) {
  activeJob.classList.remove("is-hidden");
  document.querySelector("#active-title").textContent = job.title || "生成任务";
  const status = document.querySelector("#active-status");
  status.textContent = job.status;
  status.className = `run-badge ${job.status.toLowerCase()}`;
  document.querySelector("#active-hash").textContent = shortHash(job.source_hash);
  const stack = job.stack || {};
  document.querySelector("#active-stack").textContent = stack.backend ? `${stack.backend} + ${stack.frontend}` : "-";
  const score = job.quality_gate?.score;
  document.querySelector("#active-score").textContent = score == null ? "pending" : `${Number(score).toFixed(1)} / 100`;
  const frontendBuild = job.result?.frontend_validation;
  document.querySelector("#active-frontend").textContent = frontendBuild?.passed ? "passed" : job.status === "FAILED" ? "failed" : "pending";
  document.querySelector("#progress-label").textContent = phaseLabel(job.phase);
  document.querySelector("#progress-bar").style.width = `${phaseProgress(job.phase)}%`;
  document.querySelector("#active-error").textContent = job.error || job.progress?.error || "";
  const download = document.querySelector("#download-link");
  download.classList.toggle("is-hidden", job.status !== "COMPLETED");
  if (job.status === "COMPLETED") download.href = `/api/jobs/${job.job_id}/download`;
  renderTimeline(job.phase);
}

function renderTimeline(phase) {
  const stages = [["Parse", "文档"], ["Plan", "方案"], ["Build", "代码"], ["Prove", "验证"], ["Deliver", "交付"]];
  const index = ["queued", "preparing-project", "score-gated-generation", "completed"].indexOf(phase);
  document.querySelector("#run-timeline").innerHTML = stages.map((stage, itemIndex) => {
    const state = itemIndex < index ? "is-done" : itemIndex === index ? "is-current" : "";
    return `<li class="${state}"><strong>${stage[0]}</strong><small>${stage[1]}</small></li>`;
  }).join("");
}

function renderJob(job) {
  const stateClass = job.status === "FAILED" ? "failed" : "";
  return `<button class="job-row" data-job-id="${job.job_id}" type="button">
    <strong>${escapeHtml(job.title || job.job_id)}</strong>
    <span>${escapeHtml(job.source_type || "upload")}</span>
    <span>${formatDate(job.created_at)}</span>
    <span class="job-state ${stateClass}">${job.status}</span>
  </button>`;
}

function phaseLabel(phase) {
  return { queued: "等待后台执行", "preparing-project": "初始化隔离项目", "score-gated-generation": "自动生成、验证和评分", completed: "交付完成", failed: "任务失败" }[phase] || phase || "处理中";
}

function phaseProgress(phase) {
  return { queued: 4, "preparing-project": 18, "score-gated-generation": 64, completed: 100, failed: 100 }[phase] || 8;
}

function shortHash(value) { return value ? `${value.slice(0, 16)}…` : "-"; }
function formatDate(value) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-"; }
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character])); }

fetch("/api/health").then((response) => {
  document.querySelector("#service-status").textContent = response.ok ? "本地引擎在线" : "引擎异常";
}).catch(() => { document.querySelector("#service-status").textContent = "引擎不可用"; });
loadJobs();
