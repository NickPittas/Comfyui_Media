// Media Load Video/Sequence — live metadata and range preview, no workflow run required.
import { app } from "../../scripts/app.js";

function ensureStatusWidget(node) {
    let widget = node.widgets?.find((w) => w.name === "status");
    if (widget) return widget;
    const element = document.createElement("section");
    Object.assign(element.style, {
        width: "100%", maxWidth: "100%", minWidth: "0", padding: "8px",
        color: "var(--fg-color, #ddd)", background: "var(--comfy-input-bg, #181818)",
        border: "1px solid var(--border-color, #444)", borderRadius: "7px",
        font: "11px ui-monospace, SFMono-Regular, Menlo, monospace",
        boxSizing: "border-box", overflow: "hidden", pointerEvents: "none",
    });
    widget = node.addDOMWidget("status", "media-status", element, {
        serialize: false, canvasOnly: false, hideOnZoom: false,
    });
    widget.computeLayoutSize = () => ({ minHeight: 154, minWidth: 340 });
    widget.serialize = false;
    renderMessage(node, "Inspecting source…");
    requestAnimationFrame(() => node.setSize?.([
        Math.max(node.size[0], 360), Math.max(node.size[1], node.computeSize()[1]),
    ]));
    return widget;
}

function renderMessage(node, value) {
    const element = ensureStatusWidget(node).element;
    element.replaceChildren();
    const message = document.createElement("div");
    message.textContent = String(value);
    Object.assign(message.style, { padding: "10px", opacity: "0.75", textAlign: "center" });
    element.append(message);
    node.setDirtyCanvas?.(true, true);
}

function metadataCard(title, accent, rows) {
    const card = document.createElement("div");
    Object.assign(card.style, {
        minWidth: "0", padding: "7px", borderRadius: "5px",
        background: "color-mix(in srgb, var(--comfy-input-bg, #181818) 82%, white 18%)",
        borderTop: `2px solid ${accent}`,
    });
    const heading = document.createElement("strong");
    heading.textContent = title;
    Object.assign(heading.style, { display: "block", marginBottom: "6px", color: accent, letterSpacing: "0.06em" });
    const grid = document.createElement("div");
    Object.assign(grid.style, { display: "grid", gridTemplateColumns: "auto minmax(0, 1fr)", gap: "3px 7px" });
    for (const [label, value] of rows) {
        const key = document.createElement("span"); key.textContent = label; key.style.opacity = "0.62";
        const data = document.createElement("span"); data.textContent = value;
        Object.assign(data.style, { textAlign: "right", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" });
        grid.append(key, data);
    }
    card.append(heading, grid);
    return card;
}

function renderInfo(node, info) {
    const element = ensureStatusWidget(node).element;
    const cards = document.createElement("div");
    Object.assign(cards.style, { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "7px" });
    const sourceDuration = info.source_duration == null ? "n/a" : `${info.source_duration.toFixed(3)} s`;
    const sourceFps = info.source_fps == null ? "n/a" : info.source_fps.toFixed(3);
    cards.append(
        metadataCard("ORIGINAL", "#6aa9ff", [
            ["Frames", String(info.source_count)], ["Duration", sourceDuration],
            ["Size", `${info.source_width} × ${info.source_height}`], ["FPS", sourceFps],
        ]),
        metadataCard("GENERATED", "#78d89b", [
            ["Frames", String(info.generated_count)], ["Duration", `${info.generated_duration.toFixed(3)} s`],
            ["Size", `${info.generated_width} × ${info.generated_height}`], ["FPS", String(info.output_fps)],
        ]),
    );
    const selection = document.createElement("div");
    selection.textContent = `IN ${info.first_frame}  ·  OUT ${info.source_last_frame}  ·  ${info.preset}`;
    Object.assign(selection.style, {
        marginTop: "7px", paddingTop: "6px", borderTop: "1px solid var(--border-color, #444)",
        textAlign: "center", opacity: "0.82", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
    });
    element.replaceChildren(cards, selection);
    node.setDirtyCanvas?.(true, true);
}

function widget(node, name) {
    return node.widgets?.find((item) => item.name === name);
}

function repairLegacyWidgetValues(node) {
    const colors = node.widgets?.filter((item) => item.name === "pad_color") ?? [];
    const valid = colors.map((item) => item.value).find((value) => /^#[0-9a-f]{6}$/i.test(value)) ?? "#000000";
    for (const color of colors) color.value = valid;
}

function installVideoUploadPicker(node) {
    let uploadButton = widget(node, "upload");
    const fileWidget = widget(node, "video_file");
    const sourceMode = widget(node, "source_mode");
    if (!fileWidget) return;
    if (!uploadButton) {
        uploadButton = node.addWidget("button", "upload", "Choose MOV/MP4 to upload", () => {}, { serialize: false });
        uploadButton.serialize = false;
    }
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = ".mov,.mp4,video/quicktime,video/mp4";
    uploadButton.label = "Choose MOV/MP4 to upload";
    fileInput.onchange = async () => {
        const file = fileInput.files?.[0];
        fileInput.value = "";
        if (!file) return;
        if (!/\.(mov|mp4)$/i.test(file.name)) {
            renderMessage(node, "UPLOAD ERROR: choose a MOV or MP4 file");
            return;
        }
        renderMessage(node, `Uploading ${file.name}…`);
        try {
            const result = await new Promise((resolve, reject) => {
                const request = new XMLHttpRequest();
                request.open("POST", `/media-loader/upload?filename=${encodeURIComponent(file.name)}`);
                request.upload.onprogress = (event) => {
                    const total = event.total || file.size;
                    const percent = total ? (event.loaded / total * 100).toFixed(1) : "0.0";
                    renderMessage(node, `UPLOADING ${file.name} · ${percent}% · ${(event.loaded / 1073741824).toFixed(2)} / ${(total / 1073741824).toFixed(2)} GiB`);
                };
                request.onload = () => {
                    let body;
                    try { body = JSON.parse(request.responseText); }
                    catch { reject(new Error(`${request.status} ${request.statusText}`)); return; }
                    if (request.status >= 200 && request.status < 300) resolve(body);
                    else reject(new Error(body.error || `${request.status} ${request.statusText}`));
                };
                request.onerror = () => reject(new Error("network error"));
                request.onabort = () => reject(new Error("upload cancelled"));
                request.send(file);
            });
            const value = result.name;
            const values = fileWidget.options?.values;
            if (Array.isArray(values) && !values.includes(value)) values.push(value);
            const oldFile = fileWidget.value; fileWidget.value = value;
            fileWidget.callback?.(value); node.onWidgetChanged?.(fileWidget.name, value, oldFile, fileWidget);
            if (sourceMode) {
                const oldMode = sourceMode.value; sourceMode.value = "upload";
                sourceMode.callback?.("upload"); node.onWidgetChanged?.(sourceMode.name, "upload", oldMode, sourceMode);
            }
        } catch (error) {
            renderMessage(node, `UPLOAD ERROR: ${error.message}`);
        }
    };
    uploadButton.callback = () => fileInput.click();
    const onRemoved = node.onRemoved;
    node.onRemoved = function () {
        fileInput.onchange = null;
        onRemoved?.apply(this, arguments);
    };
}

function installFrameSnapping(node, changed) {
    const first = widget(node, "first_frame");
    const last = widget(node, "last_frame");
    const preset = widget(node, "preset");
    let snapping = false;

    const snapLast = () => {
        if (snapping || Number(last.value) === -1 || preset.value === "None") return;
        const divisor = preset.value === "WAN" ? 4 : preset.value === "LTX 2.3" ? 8 : 0;
        const firstFrame = Number(first.value);
        const lastFrame = Number(last.value);
        if (!divisor || !Number.isInteger(firstFrame) || !Number.isInteger(lastFrame) || lastFrame < firstFrame) return;
        const duration = lastFrame - firstFrame + 1;
        const snappedLast = firstFrame + Math.ceil((duration - 1) / divisor) * divisor;
        if (snappedLast === lastFrame) return;
        snapping = true;
        last.value = snappedLast;
        node.onWidgetChanged?.(last.name, snappedLast, lastFrame, last);
        snapping = false;
    };

    for (const input of [first, last, preset]) {
        const callback = input.callback;
        input.callback = function (value, ...args) {
            const result = callback?.call(this, value, ...args);
            snapLast();
            changed();
            return result;
        };
    }
    snapLast();
}

function installLiveMedia(node) {
    let info;
    let previewVisible = false;
    let probeTimer;
    let loopTimer;
    const previewContainer = document.createElement("div");
    const videoStage = document.createElement("div");
    const video = document.createElement("video");
    Object.assign(previewContainer.style, {
        width: "100%", minWidth: "0", overflow: "hidden", display: "flex", flexDirection: "column",
        borderRadius: "7px", background: "#000", boxSizing: "border-box",
        border: "1px solid var(--border-color, #444)",
    });
    Object.assign(videoStage.style, {
        flex: "1 1 auto", minHeight: "180px", minWidth: "0", overflow: "hidden", background: "#000",
    });
    Object.assign(video.style, { display: "block", width: "100%", height: "100%", objectFit: "contain" });
    video.playsInline = true;
    video.muted = true;
    videoStage.append(video);
    previewContainer.append(videoStage);
    node.videoContainer = previewContainer;
    const previewWidget = node.addDOMWidget("media_preview", "video", previewContainer, {
        serialize: false, canvasOnly: false, hideOnZoom: false,
    });
    previewWidget.serialize = false;
    previewWidget.computeLayoutSize = () => ({ minHeight: previewVisible ? 228 : 0, minWidth: 360 });
    let frameCallback;
    let holdAnimation;
    let controlsBar;
    let playButton;
    let muteButton;
    let seekInput;
    let volumeInput;
    let timeLabel;
    let scrubbing = false;
    let resumeAfterScrub = false;
    let requestNumber = 0;

    const range = () => {
        if (!info?.source_fps) return;
        return {
            start: info.first_frame / info.source_fps,
            last: info.source_last_frame / info.source_fps,
            end: (info.source_last_frame + 1) / info.source_fps,
        };
    };

    const generatedDuration = () => Math.max(0, Number(info?.generated_duration) || 0);
    const selectedDuration = () => Math.max(0, Number(info?.selected_count) / Number(info?.output_fps) || 0);
    const formatTime = (seconds) => {
        const value = Math.max(0, Number(seconds) || 0);
        const minutes = Math.floor(value / 60);
        return `${minutes}:${(value % 60).toFixed(1).padStart(4, "0")}`;
    };
    const clearLoop = () => {
        clearTimeout(loopTimer);
        loopTimer = undefined;
        if (holdAnimation) cancelAnimationFrame(holdAnimation);
        holdAnimation = undefined;
    };
    const updateControls = () => {
        const playing = !video.paused || Boolean(loopTimer);
        if (playButton) {
            playButton.textContent = playing ? "⏸" : "▶";
            playButton.title = playing ? "Pause" : "Play";
        }
        if (muteButton) {
            muteButton.textContent = video.muted || video.volume === 0 ? "🔇" : "🔊";
            muteButton.title = video.muted ? "Unmute" : "Mute";
        }
        if (volumeInput) volumeInput.value = String(video.volume);
    };
    const updateTime = (elapsed) => {
        const duration = generatedDuration();
        const current = Math.min(duration, Math.max(0, elapsed));
        if (timeLabel) timeLabel.textContent = `${formatTime(current)} / ${formatTime(duration)}`;
        if (seekInput && !scrubbing) seekInput.value = String(current);
    };
    const seekToElapsed = (elapsed) => {
        const bounds = range();
        if (!bounds) return;
        const sourceTime = bounds.start + elapsed * (video.playbackRate || 1);
        video.currentTime = Math.min(bounds.last, Math.max(bounds.start, sourceTime));
        updateTime(elapsed);
    };
    const restartAtStart = () => {
        const bounds = range();
        clearLoop();
        if (!bounds) return;
        video.currentTime = bounds.start;
        updateTime(0);
        video.play().catch(() => updateControls());
    };
    const holdThenRestart = (elapsed) => {
        const duration = generatedDuration();
        const remaining = Math.max(0, duration - elapsed);
        video.pause();
        seekToElapsed(selectedDuration());
        updateTime(elapsed);
        if (remaining <= 0.001) return restartAtStart();
        const started = performance.now();
        const tick = (now) => {
            if (!loopTimer) return;
            updateTime(Math.min(duration, elapsed + (now - started) / 1000));
            holdAnimation = requestAnimationFrame(tick);
        };
        loopTimer = setTimeout(restartAtStart, remaining * 1000);
        holdAnimation = requestAnimationFrame(tick);
        updateControls();
    };
    const playFromElapsed = (elapsed) => {
        const duration = generatedDuration();
        clearLoop();
        if (elapsed >= duration - 0.001) elapsed = 0;
        if (elapsed >= selectedDuration() - 0.001) return holdThenRestart(elapsed);
        seekToElapsed(elapsed);
        video.play().catch(() => updateControls());
    };

    const attachControls = () => {
        const buttonStyle = {
            width: "28px", height: "28px", padding: "0", flex: "0 0 28px",
            color: "var(--fg-color, #ddd)", background: "transparent", border: "0",
            borderRadius: "4px", font: "14px system-ui, sans-serif", cursor: "pointer",
        };
        controlsBar = document.createElement("div");
        controlsBar.dataset.mediaLoaderControls = "true";
        Object.assign(controlsBar.style, {
            position: "static", flex: "0 0 38px", minWidth: "0", boxSizing: "border-box",
            display: "flex", alignItems: "center", gap: "6px", padding: "5px 7px",
            background: "var(--comfy-input-bg, #1b1b1b)", borderTop: "1px solid var(--border-color, #444)",
            pointerEvents: "auto",
        });
        playButton = document.createElement("button");
        seekInput = document.createElement("input");
        timeLabel = document.createElement("span");
        muteButton = document.createElement("button");
        volumeInput = document.createElement("input");
        Object.assign(playButton.style, buttonStyle); Object.assign(muteButton.style, buttonStyle);
        Object.assign(seekInput, { type: "range", min: "0", max: "0", step: "0.01", value: "0" });
        Object.assign(seekInput.style, { flex: "1 1 auto", minWidth: "54px", accentColor: "var(--p-button-primary-background, #777)" });
        Object.assign(timeLabel.style, {
            flex: "0 0 auto", minWidth: "88px", textAlign: "center", whiteSpace: "nowrap",
            font: "11px system-ui, sans-serif", color: "var(--fg-color, #ddd)",
        });
        Object.assign(volumeInput, { type: "range", min: "0", max: "1", step: "0.05", value: "1" });
        Object.assign(volumeInput.style, { width: "52px", flex: "0 0 52px", accentColor: "var(--p-button-primary-background, #777)" });
        playButton.type = muteButton.type = "button";
        playButton.ariaLabel = "Play or pause preview";
        seekInput.ariaLabel = "Preview timeline";
        muteButton.ariaLabel = "Mute or unmute preview";
        volumeInput.ariaLabel = "Preview volume";
        for (const eventName of ["pointerdown", "mousedown", "click", "dblclick", "wheel"])
            controlsBar.addEventListener(eventName, (event) => event.stopPropagation());
        playButton.addEventListener("click", () => {
            if (!video.paused || loopTimer) {
                clearLoop(); video.pause(); updateControls();
            } else playFromElapsed(Number(seekInput.value));
        });
        seekInput.addEventListener("pointerdown", () => {
            resumeAfterScrub = !video.paused || Boolean(loopTimer);
            scrubbing = true; clearLoop(); video.pause();
        });
        seekInput.addEventListener("input", () => {
            if (!scrubbing) {
                resumeAfterScrub = !video.paused || Boolean(loopTimer);
                scrubbing = true; clearLoop(); video.pause();
            }
            seekToElapsed(Number(seekInput.value));
        });
        seekInput.addEventListener("change", () => {
            const elapsed = Number(seekInput.value);
            scrubbing = false;
            if (resumeAfterScrub) playFromElapsed(elapsed); else updateControls();
            resumeAfterScrub = false;
        });
        muteButton.addEventListener("click", () => {
            video.muted = !video.muted;
            if (!video.muted && video.volume === 0) video.volume = 0.8;
            updateControls();
        });
        volumeInput.addEventListener("input", () => {
            video.volume = Number(volumeInput.value);
            video.muted = video.volume === 0;
        });
        controlsBar.append(playButton, seekInput, timeLabel, muteButton, volumeInput);
        previewContainer.append(controlsBar);
        updateControls(); updateTime(0);
    };

    const restart = () => {
        if (!loopTimer) holdThenRestart(selectedDuration());
    };
    const enforceRange = () => {
        const bounds = range();
        if (!bounds || loopTimer || scrubbing) return;
        if (video.currentTime < bounds.start - 0.001) video.currentTime = bounds.start;
        updateTime(Math.max(0, (video.currentTime - bounds.start) / (video.playbackRate || 1)));
        if (video.currentTime >= bounds.end - 0.001) restart();
    };
    const monitorFrames = () => {
        enforceRange();
        if (video?.requestVideoFrameCallback) frameCallback = video.requestVideoFrameCallback(monitorFrames);
    };
    const applyPreview = () => {
        previewVisible = Boolean(info?.preview_url);
        previewContainer.style.display = previewVisible ? "flex" : "none";
        requestAnimationFrame(() => node.setSize?.([
            Math.max(node.size[0], 360), Math.max(node.size[1], node.computeSize()[1]),
        ]));
        clearLoop();
        if (!previewVisible) {
            video.pause(); video.removeAttribute("src"); video.load();
            return;
        }
        const url = new URL(info.preview_url, window.location.href).href;
        if (video.src !== url) video.src = url;
        video.controls = false;
        video.loop = false;
        video.autoplay = true;
        video.playbackRate = Math.min(16, Math.max(0.0625, info.output_fps / info.source_fps));
        seekInput.max = String(generatedDuration());
        seekInput.value = "0";
        const bounds = range();
        if (bounds) video.currentTime = bounds.start;
        updateTime(0);
        video.play().catch(() => updateControls());
    };

    video.addEventListener("timeupdate", enforceRange);
    video.addEventListener("ended", restart);
    video.addEventListener("play", updateControls);
    video.addEventListener("pause", updateControls);
    video.addEventListener("volumechange", updateControls);
    attachControls();
    if (video.requestVideoFrameCallback) frameCallback = video.requestVideoFrameCallback(monitorFrames);

    const payload = () => ({
        source_mode: widget(node, "source_mode").value,
        video_file: String(widget(node, "video_file").value ?? ""),
        path: String(widget(node, "path").value ?? ""),
        first_frame: Number(widget(node, "first_frame").value),
        last_frame: Number(widget(node, "last_frame").value),
        output_fps: Number(widget(node, "output_fps").value),
        preset: widget(node, "preset").value,
        pad_multiple: widget(node, "pad_multiple").value,
    });

    const probe = () => {
        clearTimeout(probeTimer);
        const current = ++requestNumber;
        renderMessage(node, "Inspecting source…");
        probeTimer = setTimeout(async () => {
            try {
                const response = await fetch("/media-loader/probe", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload()),
                });
                const result = await response.json();
                if (current !== requestNumber) return;
                if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
                info = result;
                renderInfo(node, result);
                applyPreview();
            } catch (error) {
                if (current === requestNumber) renderMessage(node, `MEDIA INFO ERROR: ${error.message}`);
            }
        }, 150);
    };

    for (const name of ["source_mode", "video_file", "path", "output_fps", "pad_multiple"]) {
        const input = widget(node, name);
        const callback = input.callback;
        input.callback = function (value, ...args) {
            const result = callback?.call(this, value, ...args);
            probe();
            return result;
        };
    }
    const onRemoved = node.onRemoved;
    node.onRemoved = function () {
        clearTimeout(probeTimer); clearLoop();
        if (frameCallback && video.cancelVideoFrameCallback) video.cancelVideoFrameCallback(frameCallback);
        video.pause();
        onRemoved?.apply(this, arguments);
    };
    return { probe, refresh: probe };
}

app.registerExtension({
    name: "media.MediaLoadVideoSequence",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "MediaLoadVideoSequence") return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            ensureStatusWidget(this);
            installVideoUploadPicker(this);
            const live = installLiveMedia(this);
            installFrameSnapping(this, live.probe);
            this.__mediaLive = live;
            live.probe();
        };
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            onConfigure?.apply(this, arguments);
            repairLegacyWidgetValues(this);
            requestAnimationFrame(() => this.__mediaLive?.refresh());
        };
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function () {
            onExecuted?.apply(this, arguments);
            this.__mediaLive?.refresh();
        };
    },
});
