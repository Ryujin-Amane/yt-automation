/**
 * YT Automation — Dashboard Controller
 *
 * Vanilla JS application that manages the dashboard UI:
 *   - Tab navigation
 *   - Statistics polling
 *   - Queue management (list, upload, remove)
 *   - Dynamic settings CRUD
 *   - Multi-channel force-upload modal
 */

(function () {
    "use strict";

    const POLL_INTERVAL_MS = 8000;
    let cachedChannels = [];
    
    // Cache for stat counter animations
    let currentStats = {
        total_channels: 0,
        queue_size: 0,
        pending_count: 0
    };

    function animateValue(element, start, end, duration = 800) {
        const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (prefersReducedMotion || start === end) {
            element.textContent = end;
            return;
        }
        
        let startTimestamp = null;
        const step = (timestamp) => {
            if (!startTimestamp) startTimestamp = timestamp;
            const progress = Math.min((timestamp - startTimestamp) / duration, 1);
            // ease-out-quart
            const easeProgress = 1 - Math.pow(1 - progress, 4);
            element.textContent = Math.floor(easeProgress * (end - start) + start);
            if (progress < 1) {
                window.requestAnimationFrame(step);
            } else {
                element.textContent = end;
            }
        };
        window.requestAnimationFrame(step);
    }

    function apiRequest(endpoint, options = {}) {
        const defaults = {
            headers: { "Content-Type": "application/json" },
        };
        return fetch(endpoint, { ...defaults, ...options })
            .then((res) => {
                if (res.status === 401) {
                    window.location.reload();
                    throw new Error("Unauthorized");
                }
                return res.json();
            });
    }

    function formatDuration(seconds) {
        if (!seconds) return "0s";
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
    }

    function timeAgo(isoString) {
        if (!isoString) return "";
        const diff = Date.now() - new Date(isoString).getTime();
        const mins = Math.floor(diff / 60000);
        if (mins < 1) return "just now";
        if (mins < 60) return `${mins}m ago`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${hrs}h ago`;
        return `${Math.floor(hrs / 24)}d ago`;
    }

    function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    }

    /* ══════════════════════════════════════════════════════
       Toast Notifications
       ══════════════════════════════════════════════════════ */

    const toastContainer = document.getElementById("toastContainer");

    function showToast(message, type = "info") {
        const toast = document.createElement("div");
        toast.className = `toast toast--${type}`;
        toast.textContent = message;
        toastContainer.appendChild(toast);

        setTimeout(() => {
            toast.classList.add("toast--out");
            toast.addEventListener("animationend", () => toast.remove());
        }, 3500);
    }

    /* ══════════════════════════════════════════════════════
       Tab Navigation
       ══════════════════════════════════════════════════════ */

    const tabButtons = document.querySelectorAll(".tabs__btn");
    const tabPanels = document.querySelectorAll(".tab-panel");

    tabButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
            const targetTab = btn.dataset.tab;

            tabButtons.forEach((b) => {
                b.classList.remove("tabs__btn--active");
                b.classList.remove("nav-item--active");
            });
            btn.classList.add("tabs__btn--active");
            btn.classList.add("nav-item--active");

            tabPanels.forEach((panel) => {
                panel.classList.remove("tab-panel--active");
                if (panel.id === `panel-${targetTab}`) {
                    panel.classList.add("tab-panel--active");
                }
            });

            // Update Header Title
            const tabTitle = btn.querySelector("span").textContent;
            document.getElementById("currentTabTitle").textContent = tabTitle;

            if (targetTab === "queue") loadQueue();
            if (targetTab === "settings") loadSettings();
            if (targetTab === "scheduler") loadSchedule();
            if (targetTab === "dashboard") loadStats();
        });
    });

    /* ══════════════════════════════════════════════════════
       Dashboard — Statistics
       ══════════════════════════════════════════════════════ */

    function loadStats() {
        apiRequest("/api/stats")
            .then((data) => {
                animateValue(document.getElementById("statChannels"), currentStats.total_channels, data.total_channels || 0);
                animateValue(document.getElementById("statQueue"), currentStats.queue_size, data.queue_size || 0);
                animateValue(document.getElementById("statPending"), currentStats.pending_count, data.pending_count || 0);
                
                currentStats.total_channels = data.total_channels || 0;
                currentStats.queue_size = data.queue_size || 0;
                currentStats.pending_count = data.pending_count || 0;

                document.getElementById("statAutoUpload").textContent = data.auto_upload ? "ON" : "OFF";
                document.getElementById("queueBadge").textContent = data.pending_count || 0;

                const serverTime = new Date(data.server_time);
                document.getElementById("serverTime").textContent =
                    `UTC ${serverTime.toLocaleTimeString()} • ${data.total_channels} channel(s)`;

                const today = new Date().toLocaleDateString("en-US", {
                    weekday: "short", month: "short", day: "numeric",
                });
                document.getElementById("channelDate").textContent = today;

                renderChannels(data.channels || []);
                renderAccounts(data.monitored_accounts || []);

                cachedChannels = data.channels || [];
            })
            .catch((err) => console.error("Failed to load stats:", err));
    }

    function renderChannels(channels) {
        const container = document.getElementById("channelDetails");
        if (!channels.length) {
            container.innerHTML = '<p class="placeholder-text">No YouTube channels configured</p>';
            return;
        }

        container.innerHTML = channels
            .map((ch) => {
                const pct = ch.daily_limit > 0
                    ? Math.round((ch.uploads_today / ch.daily_limit) * 100)
                    : 0;
                return `
                    <div class="channel-row">
                        <span class="channel-row__name">Channel ${ch.channel}</span>
                        <div class="channel-row__bar-wrapper">
                            <div class="channel-row__bar" style="width: ${pct}%"></div>
                        </div>
                        <span class="channel-row__count">${ch.uploads_today} / ${ch.daily_limit}</span>
                    </div>`;
            })
            .join("");
    }

    function renderAccounts(accounts) {
        const container = document.getElementById("monitoredAccounts");
        if (!accounts.length) {
            container.innerHTML = '<p class="placeholder-text">No TikTok accounts configured</p>';
            return;
        }

        container.innerHTML = `<div class="tags-container">${accounts
            .map((a) => `<span class="tag">${escapeHtml(a)}</span>`)
            .join("")}</div>`;
    }

    /* ══════════════════════════════════════════════════════
       Queue
       ══════════════════════════════════════════════════════ */

    function loadQueue() {
        apiRequest("/api/queue")
            .then((data) => {
                renderQueue(data.queue || []);
            })
            .catch((err) => console.error("Failed to load queue:", err));
    }

    function renderQueue(queue) {
        const container = document.getElementById("queueList");
        const countEl = document.getElementById("queueCount");
        const badgeEl = document.getElementById("queueBadge");
        
        const pendingCount = queue.filter((v) => v.status === "pending").length;
        countEl.textContent = `${queue.length} video${queue.length !== 1 ? "s" : ""}`;
        badgeEl.textContent = pendingCount;

        if (!queue.length) {
            container.innerHTML = `
                <div class="queue-empty">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="queue-empty__icon"><path d="M20 6L9 17l-5-5"/></svg>
                    <p>Queue is empty</p>
                    <span>Click "Fetch Now" to scan for new TikToks</span>
                </div>`;
            return;
        }

        container.innerHTML = queue
            .map((video) => {
                const statusClass = video.status || "pending";
                const coverStyle = video.cover
                    ? `background-image: url('${video.cover}')`
                    : "";
                
                const cStatus = video.channel_status || {};
                const targetText = video.target_channels && video.target_channels.length 
                    ? `CH: ${video.target_channels.join(', ')}`
                    : "Auto Targets";
                
                const channelPills = Object.keys(cStatus).map(ch => {
                    const st = cStatus[ch];
                    const color = st === 'uploaded' ? 'var(--accent-success)' 
                                : st === 'failed' ? 'var(--accent-danger)'
                                : st === 'uploading' ? 'var(--accent-primary)'
                                : 'var(--accent-warning)';
                    return `<span style="color:${color}; font-weight:600; font-size:0.7rem; margin-right:4px;">CH${ch}:${st}</span>`;
                }).join('');

                const isActionable = video.status === "pending" || video.status === "failed";

                return `
                    <div class="queue-item queue-item--${statusClass}" data-queue-id="${video.queue_id}">
                        <div class="queue-item__thumb" style="${coverStyle}">
                            <span class="queue-item__duration">${formatDuration(video.duration)}</span>
                        </div>
                        <div class="queue-item__info">
                            <div class="queue-item__title" title="${escapeHtml(video.caption)}">${escapeHtml(video.caption || video.tiktok_id)}</div>
                            <div class="queue-item__meta">
                                <span class="queue-item__author">@${escapeHtml(video.author)}</span>
                                <span class="queue-item__status queue-item__status--${statusClass}">${statusClass}</span>
                                <span class="queue-item__channel">${targetText}</span>
                                ${channelPills ? `<div>${channelPills}</div>` : ''}
                                ${video.added_at ? `<span class="queue-item__channel">${timeAgo(video.added_at)}</span>` : ""}
                                ${video.error ? `<span class="queue-item__channel" style="color:var(--accent-rose)">${escapeHtml(video.error)}</span>` : ""}
                            </div>
                        </div>
                        <div class="queue-item__actions">
                            ${isActionable ? `
                                <button class="btn btn--icon btn--upload" onclick="window.dashboardOpenUploadModal('${video.queue_id}')" title="Force Upload">
                                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                                </button>
                            ` : ""}
                            <button class="btn btn--icon btn--remove" onclick="window.dashboardRemoveFromQueue('${video.queue_id}')" title="Remove">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                            </button>
                        </div>
                    </div>`;
            })
            .join("");
    }

    window.dashboardRemoveFromQueue = function (queueId) {
        apiRequest(`/api/queue/${queueId}/remove`, { method: "POST" })
            .then((res) => {
                if (res.success) {
                    showToast("Video removed from queue", "success");
                    loadQueue();
                    loadStats();
                } else showToast("Failed to remove video", "error");
            })
            .catch(() => showToast("Network error", "error"));
    };

    document.getElementById("btnClearCompleted").addEventListener("click", () => {
        apiRequest("/api/queue/clear", { method: "POST" })
            .then(() => {
                showToast("Completed items cleared", "success");
                loadQueue();
                loadStats();
            })
            .catch(() => showToast("Failed to clear", "error"));
    });

    document.getElementById("btnClearAll").addEventListener("click", () => {
        if (!confirm("Clear ALL items from the queue?")) return;
        apiRequest("/api/queue/clear-all", { method: "POST" })
            .then(() => {
                showToast("Queue cleared", "success");
                loadQueue();
                loadStats();
            })
            .catch(() => showToast("Failed to clear", "error"));
    });

    /* ══════════════════════════════════════════════════════
       Upload Modal (Multi-Channel)
       ══════════════════════════════════════════════════════ */

    const uploadModal = document.getElementById("uploadModal");
    const modalClose = document.getElementById("modalClose");
    const modalCancel = document.getElementById("modalCancel");
    const modalConfirm = document.getElementById("modalConfirm");
    const modalChannelContainer = document.getElementById("modalChannelContainer");
    let currentUploadQueueId = null;

    window.dashboardOpenUploadModal = function (queueId) {
        currentUploadQueueId = queueId;

        apiRequest("/api/queue")
            .then((data) => {
                const video = (data.queue || []).find((v) => v.queue_id === queueId);
                if (!video) return showToast("Video not found", "error");

                document.getElementById("modalVideoTitle").textContent = video.caption || video.tiktok_id;
                document.getElementById("modalVideoAuthor").textContent = `@${video.author}`;

                modalChannelContainer.innerHTML = '';
                const activeTargets = video.target_channels || [];

                // Render checkbox for each known channel
                cachedChannels.forEach((ch) => {
                    const isChecked = activeTargets.includes(ch.channel);
                    modalChannelContainer.innerHTML += `
                        <label class="checkbox-item">
                            <input type="checkbox" class="channel-cb" value="${ch.channel}" ${isChecked ? 'checked' : ''}>
                            <span>Channel ${ch.channel} (Limit: ${ch.daily_limit}/day)</span>
                        </label>
                    `;
                });

                uploadModal.classList.add("modal-overlay--open");
            });
    };

    function closeModal() {
        uploadModal.classList.remove("modal-overlay--open");
        currentUploadQueueId = null;
    }

    modalClose.addEventListener("click", closeModal);
    modalCancel.addEventListener("click", closeModal);
    uploadModal.addEventListener("click", (e) => {
        if (e.target === uploadModal) closeModal();
    });

    modalConfirm.addEventListener("click", () => {
        if (!currentUploadQueueId) return;

        const checkboxes = document.querySelectorAll(".channel-cb");
        const selectedChannels = Array.from(checkboxes)
            .filter(cb => cb.checked)
            .map(cb => parseInt(cb.value, 10));

        modalConfirm.disabled = true;
        modalConfirm.innerHTML = '<span class="spinner"></span> Uploading...';

        apiRequest(`/api/upload/${currentUploadQueueId}`, {
            method: "POST",
            body: JSON.stringify({ target_channels: selectedChannels }),
        })
            .then((res) => {
                if (res.success) {
                    showToast("Upload started in background", "success");
                } else {
                    showToast(res.error || "Upload failed", "error");
                }
            })
            .catch(() => showToast("Network error", "error"))
            .finally(() => {
                closeModal();
                modalConfirm.disabled = false;
                modalConfirm.innerHTML = `
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                    Upload Now`;
                loadQueue();
                loadStats();
            });
    });

    /* ══════════════════════════════════════════════════════
       Header Actions
       ══════════════════════════════════════════════════════ */

    document.getElementById("btnFetch").addEventListener("click", function () {
        this.disabled = true;
        this.innerHTML = '<span class="spinner"></span> Fetching...';
        apiRequest("/api/fetch", { method: "POST" })
            .then((res) => showToast(res.message || "Fetch triggered", "info"))
            .catch(() => showToast("Fetch failed", "error"))
            .finally(() => {
                setTimeout(() => {
                    this.disabled = false;
                    this.innerHTML = `
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M21 12a9 9 0 11-3.36-7.01"/><polyline points="21 3 21 9 15 9"/></svg>
                        <span>Fetch Now</span>`;
                    loadQueue();
                    loadStats();
                }, 3000);
            });
    });

    document.getElementById("btnUploadAll").addEventListener("click", function () {
        if (!confirm("Upload all pending videos to their assigned channels?")) return;
        this.disabled = true;
        this.innerHTML = '<span class="spinner"></span> Uploading...';
        apiRequest("/api/upload-all", { method: "POST" })
            .then((res) => showToast(res.message || "Batch upload started", "success"))
            .catch(() => showToast("Upload failed", "error"))
            .finally(() => {
                setTimeout(() => {
                    this.disabled = false;
                    this.innerHTML = `
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                        <span>Upload All</span>`;
                    loadQueue();
                    loadStats();
                }, 3000);
            });
    });

    /* ══════════════════════════════════════════════════════
       Settings
       ══════════════════════════════════════════════════════ */

    const GLOBAL_SETTING_FIELDS = [
        "check_interval_minutes",
        "max_uploads_per_channel",
        "uploads_per_channel_per_run",
        "video_max_age_hours",
        "unlimited_channel",
        "speed_factor",
        "zoom_percent",
        "saturation_boost",
        "contrast_boost",
        "ai_primary_provider",
        "custom_hashtags",
        "tiktok_usernames",
        "auto_upload_enabled",
        "color_profile",
        "video_preset"
    ];

    function loadSettings() {
        apiRequest("/api/settings")
            .then((data) => {
                const settings = data.settings || {};
                
                // Populate global fields
                GLOBAL_SETTING_FIELDS.forEach((key) => {
                    const el = document.getElementById(`setting_${key}`);
                    if (!el) return;

                    if (key === "auto_upload_enabled") {
                        el.checked = Boolean(settings[key]);
                    } else if (key === "tiktok_usernames") {
                        const names = settings[key];
                        el.value = Array.isArray(names) ? names.join(", ") : (names || "");
                    } else {
                        el.value = settings[key] !== undefined ? settings[key] : "";
                    }
                });

                // Generate and populate dynamic channel fields
                const channelConfig = settings.channel_settings || {};
                const container = document.getElementById("dynamicChannelSettings");
                container.innerHTML = "";

                if (cachedChannels.length > 0) {
                    cachedChannels.forEach(ch => {
                        const chStr = ch.channel.toString();
                        const conf = channelConfig[chStr] || {};
                        const max_uploads = conf.max_uploads_per_day !== undefined ? conf.max_uploads_per_day : (settings.max_uploads_per_channel || 3);
                        const uploads_per_run = conf.uploads_per_run !== undefined ? conf.uploads_per_run : (settings.uploads_per_channel_per_run || 1);
                        const interval = conf.upload_interval_minutes !== undefined ? conf.upload_interval_minutes : (settings.check_interval_minutes || 60);

                        container.innerHTML += `
                            <div class="card" style="margin-bottom: var(--sp-md);">
                                <div class="card__header">
                                    <h3 class="card__title font-heading">Channel ${chStr} Limits</h3>
                                </div>
                                <div class="card__body settings-grid">
                                    <div class="field">
                                        <label class="field__label">Daily Upload Limit</label>
                                        <input class="field__input dyn-ch-input" data-ch="${chStr}" data-key="max_uploads_per_day" type="number" min="1" max="50" value="${max_uploads}">
                                    </div>
                                    <div class="field">
                                        <label class="field__label">Uploads Per Run</label>
                                        <input class="field__input dyn-ch-input" data-ch="${chStr}" data-key="uploads_per_run" type="number" min="1" max="10" value="${uploads_per_run}">
                                    </div>
                                    <div class="field">
                                        <label class="field__label">Cooldown Interval (mins)</label>
                                        <input class="field__input dyn-ch-input" data-ch="${chStr}" data-key="upload_interval_minutes" type="number" min="1" value="${interval}">
                                    </div>
                                </div>
                            </div>
                        `;
                    });
                }
            })
            .catch((err) => console.error("Failed to load settings:", err));
    }

    document.getElementById("settingsForm").addEventListener("submit", (e) => {
        e.preventDefault();
        const payload = { channel_settings: {} };

        // Gather globals
        GLOBAL_SETTING_FIELDS.forEach((key) => {
            const el = document.getElementById(`setting_${key}`);
            if (!el) return;
            if (key === "auto_upload_enabled") {
                payload[key] = el.checked;
            } else if (key === "tiktok_usernames") {
                payload[key] = el.value.split(",").map((s) => s.trim()).filter(Boolean);
            } else {
                payload[key] = el.value;
            }
        });

        // Gather dynamic channels
        document.querySelectorAll(".dyn-ch-input").forEach(input => {
            const ch = input.dataset.ch;
            const key = input.dataset.key;
            if (!payload.channel_settings[ch]) payload.channel_settings[ch] = {};
            payload.channel_settings[ch][key] = parseInt(input.value, 10);
        });

        const btnSave = document.getElementById("btnSaveSettings");
        btnSave.disabled = true;
        btnSave.innerHTML = '<span class="spinner"></span> Saving...';

        apiRequest("/api/settings", {
            method: "POST",
            body: JSON.stringify(payload),
        })
            .then((res) => {
                if (res.success) {
                    showToast("Settings saved successfully", "success");
                    loadStats();
                } else showToast(res.error || "Failed to save", "error");
            })
            .catch(() => showToast("Network error", "error"))
            .finally(() => {
                btnSave.disabled = false;
                btnSave.innerHTML = `
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                    Save Settings`;
            });
    });

    document.getElementById("btnResetSettings").addEventListener("click", () => {
        if (!confirm("Reset all settings to defaults?")) return;
        apiRequest("/api/settings/reset", { method: "POST" })
            .then((res) => {
                if (res.success) {
                    showToast("Settings reset to defaults", "success");
                    loadSettings();
                    loadStats();
                }
            })
            .catch(() => showToast("Reset failed", "error"));
    });

    /* ══════════════════════════════════════════════════════
       Scheduler
       ══════════════════════════════════════════════════════ */

    let cachedScheduleData = null;
    const DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
    const ACTIVITY_LEVELS = ["VERY_HIGH", "HIGH", "MEDIUM", "LOW"];

    function loadSchedule() {
        apiRequest("/api/schedule")
            .then((data) => {
                cachedScheduleData = data.schedules || { channels: {} };
                const availChannels = data.available_channels || [];
                renderScheduleChannelConfig(cachedScheduleData, availChannels);
                renderPeakWindowsEditor(cachedScheduleData);
            })
            .catch((err) => console.error("Failed to load schedule:", err));
    }

    function renderScheduleChannelConfig(schedData, availChannels) {
        const container = document.getElementById("scheduleChannelConfig");
        if (!availChannels.length) {
            container.innerHTML = '<div class="sched-empty"><p>No YouTube channels configured</p></div>';
            return;
        }

        const channelsCfg = schedData.channels || {};
        let html = '<div class="sched-channel-grid">';

        for (const ch of availChannels) {
            const chKey = ch.toString();
            const cfg = channelsCfg[chKey] || {};

            // Resolve inherited values
            let resolved = { ...cfg };
            if (cfg.inherit_from) {
                const parentCfg = channelsCfg[cfg.inherit_from.toString()] || {};
                resolved = { ...parentCfg, ...cfg };
            }

            const name = resolved.name || `Channel ${ch}`;
            const tz = resolved.timezone || "Asia/Karachi";
            const stagger = resolved.channel_stagger_minutes || 0;
            const maxBatch = resolved.max_videos_per_batch || 7;
            const inheritsFrom = cfg.inherit_from || "";

            html += `
                <div class="sched-channel-card" data-sch-ch="${chKey}">
                    <div class="sched-channel-card__header">
                        <span class="sched-channel-card__name">CH${chKey}</span>
                        <span class="sched-channel-card__tz">${escapeHtml(tz)}</span>
                    </div>
                    <div class="field">
                        <label class="field__label">Channel Name</label>
                        <input class="field__input sch-input" data-sch-ch="${chKey}" data-sch-key="name" type="text" value="${escapeHtml(name)}">
                    </div>
                    <div class="field">
                        <label class="field__label">Timezone</label>
                        <input class="field__input sch-input" data-sch-ch="${chKey}" data-sch-key="timezone" type="text" value="${escapeHtml(tz)}">
                    </div>
                    <div class="field">
                        <label class="field__label">Stagger (minutes offset)</label>
                        <input class="field__input sch-input" data-sch-ch="${chKey}" data-sch-key="channel_stagger_minutes" type="number" min="0" max="120" value="${stagger}">
                    </div>
                    <div class="field">
                        <label class="field__label">Max Videos / Batch</label>
                        <input class="field__input sch-input" data-sch-ch="${chKey}" data-sch-key="max_videos_per_batch" type="number" min="1" max="50" value="${maxBatch}">
                    </div>
                    <div class="field">
                        <label class="field__label">Inherit Schedule From</label>
                        <select class="field__select sch-input" data-sch-ch="${chKey}" data-sch-key="inherit_from">
                            <option value="">None (own schedule)</option>
                            ${availChannels.filter(c => c !== ch).map(c => 
                                `<option value="${c}" ${inheritsFrom.toString() === c.toString() ? 'selected' : ''}>Channel ${c}</option>`
                            ).join('')}
                        </select>
                        <span class="field__hint">Inherits peak windows from another channel</span>
                    </div>
                </div>`;
        }

        html += '</div>';
        container.innerHTML = html;
    }

    function renderPeakWindowsEditor(schedData) {
        const container = document.getElementById("peakWindowsEditor");
        const channelsCfg = schedData.channels || {};

        // Use Channel 1 windows as the baseline (other channels inherit)
        const ch1Cfg = channelsCfg["1"] || {};
        const windows = ch1Cfg.peak_viewer_windows || [
            { day: "Monday", activity_level: "HIGH", recommended_publish_pkt: "16:30" },
            { day: "Tuesday", activity_level: "HIGH", recommended_publish_pkt: "17:30" },
            { day: "Wednesday", activity_level: "HIGH", recommended_publish_pkt: "17:30" },
            { day: "Thursday", activity_level: "HIGH", recommended_publish_pkt: "18:30" },
            { day: "Friday", activity_level: "HIGH", recommended_publish_pkt: "16:30" },
            { day: "Saturday", activity_level: "VERY_HIGH", recommended_publish_pkt: "17:30" },
            { day: "Sunday", activity_level: "VERY_HIGH", recommended_publish_pkt: "17:30" },
        ];

        // Build lookup by day
        const windowMap = {};
        windows.forEach(w => { windowMap[w.day] = w; });

        const dayEmojis = {
            Monday: "🟢", Tuesday: "🔵", Wednesday: "🟣",
            Thursday: "🟠", Friday: "🔴", Saturday: "⭐", Sunday: "🌟"
        };

        let html = '<div class="peak-grid">';

        for (const day of DAYS_OF_WEEK) {
            const w = windowMap[day] || { day, activity_level: "HIGH", recommended_publish_pkt: "17:30" };
            const peakStart = w.peak_start_pkt || "";
            const peakEnd = w.peak_end_pkt || "";
            const recPublish = w.recommended_publish_pkt || "17:30";
            const level = w.activity_level || "HIGH";

            html += `
                <div class="peak-day-card" data-peak-day="${day}">
                    <div class="peak-day-card__name">
                        <span>${dayEmojis[day] || "📅"}</span>
                        <span>${day}</span>
                        <span class="activity-badge activity-badge--${level.toLowerCase()}">${level}</span>
                    </div>
                    <div class="field">
                        <label class="field__label">Activity Level</label>
                        <select class="field__select peak-input" data-peak-day="${day}" data-peak-key="activity_level">
                            ${ACTIVITY_LEVELS.map(l => `<option value="${l}" ${l === level ? 'selected' : ''}>${l}</option>`).join('')}
                        </select>
                    </div>
                    <div class="field">
                        <label class="field__label">Publish Time (PKT)</label>
                        <input class="field__input peak-input" data-peak-day="${day}" data-peak-key="recommended_publish_pkt" type="time" value="${recPublish}">
                    </div>
                    <div class="field">
                        <label class="field__label">Peak Start</label>
                        <input class="field__input peak-input" data-peak-day="${day}" data-peak-key="peak_start_pkt" type="time" value="${peakStart}">
                    </div>
                    <div class="field">
                        <label class="field__label">Peak End</label>
                        <input class="field__input peak-input" data-peak-day="${day}" data-peak-key="peak_end_pkt" type="time" value="${peakEnd}">
                    </div>
                </div>`;
        }

        html += '</div>';
        container.innerHTML = html;
    }

    function gatherSchedulePayload() {
        const payload = { channels: {} };

        // Gather channel configs
        document.querySelectorAll(".sch-input").forEach(input => {
            const ch = input.dataset.schCh;
            const key = input.dataset.schKey;
            if (!payload.channels[ch]) payload.channels[ch] = {};

            if (key === "channel_stagger_minutes" || key === "max_videos_per_batch") {
                payload.channels[ch][key] = parseInt(input.value, 10) || 0;
            } else if (key === "inherit_from") {
                if (input.value) {
                    payload.channels[ch][key] = input.value;
                }
                // Don't set inherit_from if empty
            } else {
                payload.channels[ch][key] = input.value;
            }
        });

        // Gather peak windows for channel 1 (others inherit)
        const peakWindows = [];
        for (const day of DAYS_OF_WEEK) {
            const win = { day };
            document.querySelectorAll(`.peak-input[data-peak-day="${day}"]`).forEach(input => {
                const key = input.dataset.peakKey;
                win[key] = input.value;
            });
            peakWindows.push(win);
        }

        // Attach peak windows to channel 1
        if (payload.channels["1"]) {
            payload.channels["1"].peak_viewer_windows = peakWindows;
        } else {
            payload.channels["1"] = { peak_viewer_windows: peakWindows };
        }

        // Add utc_offset for all channels
        Object.keys(payload.channels).forEach(ch => {
            if (!payload.channels[ch].utc_offset) {
                payload.channels[ch].utc_offset = "+05:00";
            }
        });

        return payload;
    }

    // Save schedule
    document.getElementById("btnSaveSchedule").addEventListener("click", () => {
        const payload = gatherSchedulePayload();
        const btn = document.getElementById("btnSaveSchedule");
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Saving...';

        apiRequest("/api/schedule", {
            method: "POST",
            body: JSON.stringify(payload),
        })
            .then((res) => {
                if (res.success) {
                    showToast("Schedule saved successfully", "success");
                    loadSchedule();
                } else {
                    showToast(res.error || "Failed to save schedule", "error");
                }
            })
            .catch(() => showToast("Network error", "error"))
            .finally(() => {
                btn.disabled = false;
                btn.innerHTML = `
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                    Save Schedule`;
            });
    });

    // Preview slots
    document.getElementById("btnPreviewSlots").addEventListener("click", () => {
        const perDay = document.getElementById("batch_per_day").value;
        const maxPerCh = document.getElementById("batch_max_per_channel").value;
        const count = parseInt(maxPerCh, 10) * parseInt(perDay, 10);
        const btn = document.getElementById("btnPreviewSlots");

        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Loading...';

        apiRequest(`/api/schedule/preview?per_day=${perDay}&count=${count}`)
            .then((data) => {
                renderSlotPreview(data.channels || {});
                document.getElementById("slotPreviewCard").hidden = false;
                document.getElementById("slotPreviewMeta").textContent =
                    `${perDay} video(s)/day × ${maxPerCh} days`;
            })
            .catch(() => showToast("Failed to load preview", "error"))
            .finally(() => {
                btn.disabled = false;
                btn.innerHTML = `
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                    Preview Slots`;
            });
    });

    function renderSlotPreview(channels) {
        const container = document.getElementById("slotPreviewBody");

        if (!Object.keys(channels).length) {
            container.innerHTML = '<div class="sched-empty"><p>No channels available</p></div>';
            return;
        }

        let html = '<div class="slot-timeline">';

        for (const [chKey, chData] of Object.entries(channels)) {
            html += `
                <div class="slot-timeline__channel">
                    <div class="slot-timeline__ch-header">
                        <span class="slot-timeline__ch-name">CH${chKey} — ${escapeHtml(chData.name)}</span>
                        <span class="slot-timeline__ch-stagger">+${chData.stagger}m stagger</span>
                    </div>`;

            // Group slots by day
            const slotsByDay = {};
            (chData.slots || []).forEach(slot => {
                if (!slotsByDay[slot.day]) slotsByDay[slot.day] = [];
                slotsByDay[slot.day].push(slot);
            });

            for (const [day, slots] of Object.entries(slotsByDay)) {
                html += `
                    <div class="slot-day-group">
                        <div class="slot-day-group__label">${day}</div>
                        <div class="slot-pills">`;

                for (const slot of slots) {
                    const timeStr = slot.publish_at_local || "";
                    const label = slot.slot_label || "peak";
                    const actBadge = slot.activity_level || "HIGH";

                    html += `
                        <div class="slot-pill" title="${slot.publish_at_utc_iso || ''}">
                            <span class="slot-pill__time">${escapeHtml(timeStr)}</span>
                            <span class="slot-pill__label slot-pill__label--${label}">${label}</span>
                            <span class="activity-badge activity-badge--${actBadge.toLowerCase()}" style="font-size:0.6rem;">${actBadge}</span>
                        </div>`;
                }

                html += '</div></div>';
            }

            html += '</div>';
        }

        html += '</div>';
        container.innerHTML = html;
    }

    // Run Batch
    document.getElementById("btnRunBatch").addEventListener("click", function () {
        const perDay = parseInt(document.getElementById("batch_per_day").value, 10);
        const maxPerCh = parseInt(document.getElementById("batch_max_per_channel").value, 10);
        const dryRun = document.getElementById("batch_dry_run").checked;

        const confirmMsg = dryRun
            ? `Preview batch: ${perDay} video(s)/day × ${maxPerCh} days (DRY RUN — no uploads)?`
            : `Start batch: Fetch + Schedule + Upload ${perDay} video(s)/day × ${maxPerCh} days to ALL channels?`;

        if (!confirm(confirmMsg)) return;

        this.disabled = true;
        this.innerHTML = '<span class="spinner"></span> Running...';

        apiRequest("/api/schedule/batch", {
            method: "POST",
            body: JSON.stringify({
                per_day: perDay,
                max_per_channel: maxPerCh,
                dry_run: dryRun,
            }),
        })
            .then((res) => {
                if (res.success) {
                    showToast(res.message || "Batch started", "success");
                } else {
                    showToast(res.error || "Batch failed", "error");
                }
            })
            .catch(() => showToast("Network error", "error"))
            .finally(() => {
                setTimeout(() => {
                    this.disabled = false;
                    this.innerHTML = `
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                        Run Batch Now`;
                }, 5000);
            });
    });

    // Poll every 8 seconds
    function pollData() {
        loadStats();
        const activeTab = document.querySelector(".tabs__btn--active");
        if (activeTab && activeTab.dataset.tab === "queue") loadQueue();
    }

    loadStats();
    loadSettings();
    setInterval(pollData, POLL_INTERVAL_MS);
})();

