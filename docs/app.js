const state = {
  data: null,
  source: "Loading data",
  filters: {
    search: "",
    theme: "",
    channel: "",
  },
};

const elements = {
  dataSource: document.querySelector("#dataSource"),
  generatedAt: document.querySelector("#generatedAt"),
  channelCount: document.querySelector("#channelCount"),
  videoCount: document.querySelector("#videoCount"),
  readyCount: document.querySelector("#readyCount"),
  blockedCount: document.querySelector("#blockedCount"),
  searchInput: document.querySelector("#searchInput"),
  themeFilter: document.querySelector("#themeFilter"),
  channelFilter: document.querySelector("#channelFilter"),
  resultCount: document.querySelector("#resultCount"),
  videoRows: document.querySelector("#videoRows"),
};

function assertOk(response, label) {
  if (!response.ok) {
    throw new Error(`${label} returned ${response.status}`);
  }
  return response;
}

function loadDashboardData() {
  return fetch("/dashboard-data", { cache: "no-store" })
    .then((response) => assertOk(response, "Live API"))
    .then((response) => {
      state.source = "Live API";
      return response.json();
    })
    .catch((error) => {
      console.info("Falling back to static dashboard JSON:", error.message);
      state.source = "Static JSON";
      return fetch("./data/latest.json", { cache: "no-store" })
        .then((response) => assertOk(response, "Static JSON"))
        .then((response) => response.json());
    });
}

function normalizeText(value) {
  return String(value || "").toLowerCase();
}

function asList(value) {
  return Array.isArray(value) ? value.filter(Boolean) : [];
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

function formatDate(value) {
  if (!value) {
    return "Unknown";
  }
  return String(value).slice(0, 10);
}

function formatStatus(value) {
  return String(value || "pending").replaceAll("_", " ");
}

function statusClass(value) {
  return String(value || "pending").replaceAll("_", "-").toLowerCase();
}

function truncate(value, maxLength) {
  const text = String(value || "");
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 3)}...`;
}

function setText(element, value) {
  element.textContent = value;
}

function clearChildren(element) {
  while (element.firstChild) {
    element.removeChild(element.firstChild);
  }
}

function createChipList(items, fallback) {
  const wrapper = document.createElement("div");
  wrapper.className = "chip-list";

  const values = asList(items);
  if (values.length === 0) {
    const span = document.createElement("span");
    span.className = "muted";
    span.textContent = fallback;
    wrapper.appendChild(span);
    return wrapper;
  }

  values.slice(0, 5).forEach((item) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = item;
    wrapper.appendChild(chip);
  });

  if (values.length > 5) {
    const more = document.createElement("span");
    more.className = "chip";
    more.textContent = `+${values.length - 5}`;
    wrapper.appendChild(more);
  }

  return wrapper;
}

function createCell(className, child) {
  const cell = document.createElement("td");
  if (className) {
    cell.className = className;
  }
  if (typeof child === "string") {
    cell.textContent = child;
  } else {
    cell.appendChild(child);
  }
  return cell;
}

function createTitleCell(video) {
  const wrapper = document.createElement("div");
  const link = document.createElement("a");
  link.className = "title-link";
  link.href = video.url || "#";
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = video.title || "Untitled video";
  wrapper.appendChild(link);
  return createCell("", wrapper);
}

function createStatusCell(video) {
  const wrapper = document.createElement("div");
  const status = video.status || video.analysis_status || "pending";
  const pill = document.createElement("span");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = formatStatus(status);
  wrapper.appendChild(pill);

  const meta = document.createElement("span");
  meta.className = "status-meta";
  meta.textContent = `Transcript: ${video.transcript_source || video.transcript_status || "pending"}`;
  wrapper.appendChild(meta);

  return createCell("status-cell", wrapper);
}

function videoSearchText(video) {
  return [
    video.date,
    video.channel,
    video.title,
    video.summary,
    video.status,
    video.transcript_source,
    ...asList(video.speakers),
    ...asList(video.topics),
    ...asList(video.themes),
  ].map(normalizeText).join(" ");
}

function filteredVideos() {
  const videos = asList(state.data?.videos);
  const search = normalizeText(state.filters.search).trim();

  return videos.filter((video) => {
    const matchesSearch = !search || videoSearchText(video).includes(search);
    const matchesTheme = !state.filters.theme || asList(video.themes).includes(state.filters.theme);
    const matchesChannel = !state.filters.channel || video.channel === state.filters.channel;
    return matchesSearch && matchesTheme && matchesChannel;
  });
}

function renderStats() {
  const stats = state.data?.stats || {};
  const videos = asList(state.data?.videos);
  const ready = stats.videos?.ready ?? videos.filter((video) => video.status === "ready").length;
  const blocked = videos.filter((video) => {
    const status = video.status || video.analysis_status || "";
    return status.includes("failed") || status.includes("retryable");
  }).length;

  setText(elements.dataSource, state.source);
  setText(elements.generatedAt, `Generated: ${state.data?.generated_at || "unknown"}`);
  setText(elements.channelCount, stats.channels?.total ?? asList(state.data?.channels).length);
  setText(elements.videoCount, stats.videos?.total ?? videos.length);
  setText(elements.readyCount, ready);
  setText(elements.blockedCount, blocked);
}

function renderFilters() {
  const videos = asList(state.data?.videos);
  const themes = uniqueSorted(videos.flatMap((video) => asList(video.themes)));
  const channels = uniqueSorted(videos.map((video) => video.channel));

  const appendOptions = (select, values) => {
    const current = select.value;
    while (select.options.length > 1) {
      select.remove(1);
    }
    values.forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.appendChild(option);
    });
    select.value = values.includes(current) ? current : "";
  };

  appendOptions(elements.themeFilter, themes);
  appendOptions(elements.channelFilter, channels);
}

function renderRows() {
  const videos = filteredVideos();
  clearChildren(elements.videoRows);
  setText(elements.resultCount, `${videos.length} video${videos.length === 1 ? "" : "s"}`);

  if (videos.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.className = "empty-state";
    cell.colSpan = 8;
    cell.textContent = "No videos match the current filters.";
    row.appendChild(cell);
    elements.videoRows.appendChild(row);
    return;
  }

  videos.forEach((video) => {
    const row = document.createElement("tr");
    row.appendChild(createCell("date-cell", formatDate(video.date || video.published_at)));
    row.appendChild(createCell("channel-cell", video.channel || "Unknown channel"));
    row.appendChild(createTitleCell(video));
    row.appendChild(createCell("", createChipList(video.speakers, "Unknown")));
    row.appendChild(createCell("", createChipList(video.topics, "Pending")));
    row.appendChild(createCell("", createChipList(video.themes, "Pending")));
    row.appendChild(createCell("summary", truncate(video.summary || "No summary available yet.", 360)));
    row.appendChild(createStatusCell(video));
    elements.videoRows.appendChild(row);
  });
}

function render() {
  renderStats();
  renderFilters();
  renderRows();
}

function bindEvents() {
  elements.searchInput.addEventListener("input", (event) => {
    state.filters.search = event.target.value;
    renderRows();
  });

  elements.themeFilter.addEventListener("change", (event) => {
    state.filters.theme = event.target.value;
    renderRows();
  });

  elements.channelFilter.addEventListener("change", (event) => {
    state.filters.channel = event.target.value;
    renderRows();
  });
}

function showError(error) {
  console.error("Dashboard failed to load:", error);
  setText(elements.dataSource, "Load failed");
  clearChildren(elements.videoRows);
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.className = "empty-state";
  cell.colSpan = 8;
  cell.textContent = `Unable to load dashboard data: ${error.message}`;
  row.appendChild(cell);
  elements.videoRows.appendChild(row);
}

bindEvents();
loadDashboardData()
  .then((data) => {
    state.data = data;
    render();
  })
  .catch(showError);
