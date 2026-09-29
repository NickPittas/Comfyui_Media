import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

function element(tag, parent, style = {}) {
    const el = document.createElement(tag);
    Object.assign(el.style, style);
    parent.append(el);
    return el;
}

function setWidget(node, name, value) {
    const widget = node.widgets?.find((w) => w.name === name);
    if (!widget) return;
    const old = widget.value;
    widget.value = value;
    widget.callback?.(value);
    node.onWidgetChanged?.(name, value, old, widget);
}

function browse(node) {
    const dialog = document.createElement("dialog");
    Object.assign(dialog.style, {
        color: "var(--fg-color, #ddd)", background: "var(--comfy-input-bg, #222)",
        border: "1px solid var(--border-color, #555)", borderRadius: "8px",
        padding: "16px", minWidth: "320px", maxWidth: "min(90vw, 600px)",
    });
    const title = element("h3", dialog, { margin: "0 0 12px" });
    title.id = "media-video-browse-title";
    title.textContent = "Server output folder";
    dialog.setAttribute("aria-labelledby", title.id);
    const roots = element("select", dialog, { width: "100%", marginBottom: "10px" });
    roots.ariaLabel = "Output root";
    const location = element("div", dialog, { marginBottom: "8px", overflowWrap: "anywhere" });
    const list = element("div", dialog, { maxHeight: "40vh", overflowY: "auto" });
    const error = element("div", dialog, { color: "#f88", marginTop: "8px" });
    const actions = element("div", dialog, { display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "12px" });
    const choose = element("button", actions);
    choose.type = "button";
    choose.textContent = "Use this folder";
    const cancel = element("button", actions);
    cancel.type = "button";
    cancel.textContent = "Cancel";
    let root = String(node.widgets?.find((w) => w.name === "output_root")?.value ?? "");
    let path = String(node.widgets?.find((w) => w.name === "subfolder")?.value ?? "");
    let request = 0;
    const close = () => { ++request; dialog.close(); dialog.remove(); };
    cancel.onclick = close;
    dialog.oncancel = (event) => { event.preventDefault(); close(); };
    choose.onclick = () => { setWidget(node, "output_root", root); setWidget(node, "subfolder", path); close(); };
    roots.onchange = () => load(roots.value, "");
    const load = async (id, relative) => {
        const current = ++request;
        error.textContent = "";
        list.replaceChildren();
        choose.disabled = true;
        try {
            const query = new URLSearchParams({ path: relative });
            if (id) query.set("root", id);
            const response = await fetch(api.apiURL(`/media-video/dirs?${query}`));
            const data = await response.json();
            if (current !== request) return;
            if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
            root = id || data.roots[0]?.id || "";
            path = data.path;
            roots.replaceChildren();
            for (const option of data.roots) {
                const item = document.createElement("option");
                item.value = option.id;
                item.textContent = option.label;
                roots.append(item);
            }
            roots.value = root;
            location.textContent = `${roots.selectedOptions[0]?.textContent ?? root} / ${path}`;
            const entry = (label, destination) => {
                const button = element("button", list, { display: "block", width: "100%", textAlign: "left", marginBottom: "4px" });
                button.type = "button";
                button.textContent = label;
                button.onclick = () => load(root, destination);
            };
            if (path) entry("..", path.split("/").slice(0, -1).join("/"));
            for (const name of data.dirs) entry(`${name}/`, [path, name].filter(Boolean).join("/"));
            choose.disabled = false;
        } catch (cause) {
            if (current === request) error.textContent = `Cannot browse folder: ${cause.message}`;
        }
    };
    document.body.append(dialog);
    dialog.showModal();
    load(root, path);
    return close;
}

function install(node) {
    const box = element("div", document.createDocumentFragment(), {
        width: "100%", minWidth: "0", boxSizing: "border-box", padding: "6px",
        color: "var(--fg-color, #ddd)", background: "var(--comfy-input-bg, #181818)",
        font: "12px system-ui, sans-serif",
    });
    const stage = element("div", box, { position: "relative", width: "100%", aspectRatio: "16 / 9", background: "#000", overflow: "hidden" });
    const videoStyle = { position: "absolute", inset: "0", width: "100%", height: "100%", objectFit: "contain" };
    const video = element("video", stage, videoStyle);
    const reference = element("video", stage, { ...videoStyle, pointerEvents: "none" });
    const divider = element("div", stage, { position: "absolute", top: "0", bottom: "0", width: "2px", background: "#6aa9ff", pointerEvents: "none" });
    const empty = element("div", stage, { position: "absolute", inset: "0", display: "grid", placeItems: "center" });
    empty.textContent = "Run the node to preview video";
    video.playsInline = reference.playsInline = true;
    video.preload = reference.preload = "metadata";
    video.loop = true;
    // Muted default keeps autoplay from being rejected by browser policy.
    video.muted = true;
    reference.muted = true;
    // Timeline seek slider on its own row; frame counter rides along.
    const controls = element("div", box, { display: "flex", gap: "6px", alignItems: "center", marginTop: "6px" });
    const timeline = element("input", controls, { flex: "1", minWidth: "40px" });
    timeline.type = "range"; timeline.min = timeline.max = timeline.value = "0"; timeline.step = "1";
    timeline.disabled = true;
    timeline.ariaLabel = "Video frame";
    const frame = element("span", controls, { whiteSpace: "nowrap" });
    // Icon-only transport row; toggles show pressed state via aria-pressed + accent style.
    const row = element("div", box, { display: "flex", gap: "5px", alignItems: "center", marginTop: "6px", flexWrap: "wrap" });
    const setOn = (button, on) => {
        button.setAttribute("aria-pressed", String(on));
        button.style.background = on ? "rgba(106,169,255,0.35)" : "";
        button.style.borderColor = on ? "#6aa9ff" : "";
    };
    const icon = (glyph, label) => {
        const button = element("button", row, { width: "30px", height: "26px", padding: "0", fontSize: "14px", lineHeight: "1", display: "grid", placeItems: "center" });
        button.type = "button";
        button.textContent = glyph;
        button.title = label;
        button.ariaLabel = label;
        return button;
    };
    const play = icon("▶", "Play preview");
    const previous = icon("‹", "Previous frame");
    const next = icon("›", "Next frame");
    const loop = icon("⟳", "Loop playback");
    const mute = icon("🔇", "Unmute preview");
    const volume = element("input", row, { width: "26px", height: "44px", padding: "0" });
    const full = icon("⛶", "Fullscreen preview");
    const compare = icon("⧉", "Reference wipe comparison");
    const download = element("a", row, { width: "30px", height: "26px", fontSize: "14px", lineHeight: "1", display: "none", placeItems: "center", textDecoration: "none", color: "inherit" });
    download.title = "Save a copy"; download.ariaLabel = "Save a copy";
    play.disabled = previous.disabled = next.disabled = true;
    volume.type = "range"; volume.min = "0"; volume.max = "1"; volume.step = "0.05";
    volume.value = String(video.volume);
    volume.ariaLabel = "Volume"; volume.title = "Volume";
    volume.setAttribute("orient", "vertical");
    volume.style.writingMode = "vertical-lr"; volume.style.direction = "rtl";
    const syncMute = () => {
        // Icon stays the muted glyph as a stable reference; label + aria-pressed carry state.
        mute.textContent = "🔇";
        mute.title = mute.ariaLabel = video.muted ? "Unmute preview" : "Mute preview";
        setOn(mute, video.muted);
    };
    const syncPlay = (playing) => {
        play.textContent = playing ? "❚❚" : "▶";
        play.title = play.ariaLabel = playing ? "Pause preview" : "Play preview";
    };
    setOn(loop, video.loop);
    syncMute();
    const footer = element("div", box, { display: "flex", gap: "8px", alignItems: "center", marginTop: "6px" });
    const saved = element("span", footer, { flex: "1", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" });
    // Reported height derives from the MEASURED box height (+ small host gutter) instead of a
    // fixed floor: DOMWidgetImpl.computeLayoutSize reads ONLY getMinHeight/getMaxHeight/
    // getHeight, so those getters are the whole contract. Until first measure, fall back to
    // the old 250 floor; measured is kept fresh by the observer/onResize below.
    let measured = 0;
    const HOST_GUTTER = 4; // ComfyUI hands the widget row a few px less than asked
    const heightFor = () => (measured > 0 ? measured : 250) + HOST_GUTTER;
    const widget = node.addDOMWidget("media_video_preview", "video", box, {
        serialize: false, canvasOnly: false, hideOnZoom: false,
        getMinHeight: heightFor, getMaxHeight: heightFor, getHeight: heightFor,
    });
    widget.serialize = false;
    for (const event of ["pointerdown", "mousedown", "click", "dblclick", "wheel"])
        box.addEventListener(event, (e) => e.stopPropagation());

    // Vue nodes mode sizes the DOM widget host from its content, so the box's width:100%
    // resolves against a shrunken host. Pin min-width on the node element and on the box
    // itself (live node width less padding insets) so the box fills the node; re-applied
    // on resize via the observer/onResize below.
    const vueMode = () => Boolean(window.LiteGraph?.vueNodesMode);
    const syncWidth = () => {
        if (!vueMode()) return;
        if (box.style.width) box.style.width = "";
        const el = document.querySelector(`[data-node-id="${node.id}"]`);
        if (el && el.style.minWidth !== "340px") el.style.minWidth = "340px";
        const px = `${Math.max(0, node.size[0] - 12)}px`;
        if (box.style.minWidth !== px) box.style.minWidth = px;
    };
    // Measured-height half of the size contract: raise the node whenever the box changes
    // height (node width → 16:9/video-aspect stage, controls wrap). Raise-only and only on a
    // >1px change, so observer/setSize can't ping-pong. Fullscreen balloons the stage to the
    // viewport, and a popped-out widget lives in another document — adopt neither.
    const measure = () => {
        if (document.fullscreenElement?.contains(stage)) return;
        if (box.ownerDocument !== document) return;
        const h = box.offsetHeight; // offsetHeight is unscaled; getBoundingClientRect is zoomed
        if (h < 1) return; // pre-mount or hidden (collapsed)
        if (Math.abs(h - measured) > 1) measured = h;
        // The raise is re-checked on every syncLayout, not only when the box's height
        // changed: a manual node resize shrinks the node without touching the box.
        const needed = node.computeSize()[1];
        if (node.size[1] < needed) node.setSize([node.size[0], needed]);
    };
    // Vue-mode containment guarantee: the Vue DOM widget host can be laid out outside the
    // flow node.computeSize() models, so the node border can end above the box bottom
    // (slider/buttons dangling below the node). Measure real geometry — box bottom vs node
    // root top via getBoundingClientRect (same viewport scale) — and convert to unscaled
    // node px with the root's rect/offset ratio. Raise-only: never shrink the user's node
    // height; minHeight pin is cleared once content fits again. Idempotent assignments keep
    // observer/onResize callbacks from ping-ponging; no polling of our own.
    let vuePinned = "";
    const containVue = () => {
        if (!vueMode()) return;
        if (document.fullscreenElement?.contains(stage)) return;
        if (box.ownerDocument !== document) return;
        const el = document.querySelector(`[data-node-id="${node.id}"]`);
        if (!el) return;
        const rootRect = el.getBoundingClientRect();
        const boxRect = box.getBoundingClientRect();
        if (rootRect.width < 1 || rootRect.height < 1 || boxRect.height < 1) return;
        const scale = rootRect.width / el.offsetWidth || 1;
        const needed = Math.ceil((boxRect.bottom - rootRect.top) / scale) + 2;
        if (boxRect.bottom > rootRect.bottom + 1) {
            const css = `${needed}px`;
            if (el.style.minHeight !== css) el.style.minHeight = css;
            vuePinned = css;
            if (node.size[1] < needed) node.setSize([node.size[0], needed]);
        } else if (vuePinned && el.style.minHeight === vuePinned) {
            el.style.minHeight = "";
            vuePinned = "";
        }
    };
    // Classic canvas mode: DOM widgets are transformed onto the canvas with no flow
    // layout and no clipping, so a manually shrunk node lets the preview/timeline hang
    // below the border (containVue only covers Vue nodes mode). LiteGraph's
    // _arrangeWidgets writes each widget's node-relative y on draw; the preview's y plus
    // its unzoomed box height plus a small bottom inset is the height the node must
    // keep. Raise-only: never shrink the user's node.
    const containClassic = () => {
        if (vueMode()) return;
        if (document.fullscreenElement?.contains(stage)) return;
        if (box.ownerDocument !== document) return;
        const y = widget.y;
        if (!Number.isFinite(y) || box.offsetHeight < 1) return; // not laid out yet / hidden
        // +12: LiteGraph positions actual content ~10px below widget.y (DEFAULT_MARGIN),
        // and box measures the widget box, not the DOM controls hanging out of it.
        const needed = Math.ceil(y + box.offsetHeight) + 12;
        if (node.size[1] < needed) node.setSize([node.size[0], needed]);
    };
    const syncLayout = () => { syncWidth(); measure(); containVue(); containClassic(); };
    const widthObserver = new ResizeObserver(syncLayout);
    widthObserver.observe(box);
    const origOnResize = node.onResize;
    node.onResize = function (...args) {
        origOnResize?.apply(this, args);
        syncLayout();
    };
    // widget.y only exists after LiteGraph's first layout pass draws the node; retry
    // syncLayout for a few animation frames, then stop — the observer/onResize callbacks
    // cover everything after (no polling).
    let rafHandle = 0;
    let layoutRetries = 0;
    const syncWhenLaidOut = () => {
        cancelAnimationFrame(rafHandle);
        layoutRetries = 12;
        const step = () => {
            rafHandle = 0;
            syncLayout();
            // LiteGraph initializes widget.y = 0 before the first layout pass, so
            // "finite" alone stops the retry too early; treat y <= 0 as not laid out.
            if (layoutRetries > 0 && !(widget.y > 0)) {
                --layoutRetries;
                rafHandle = requestAnimationFrame(step);
            }
        };
        rafHandle = requestAnimationFrame(step);
    };
    node.__mediaVideoSync = syncWhenLaidOut;
    syncLayout();
    syncWhenLaidOut();

    let fps = 24;
    let frames = 0;
    let comparing = false;
    let wipe = 0.5;
    let scrubbing = false;
    let closeBrowser;
    const sync = (force = false) => {
        if (!comparing || reference.readyState < 1) return;
        // Skip drift chasing once a shorter reference has run out; a forced seek resumes it.
        if (force || (Math.abs(reference.currentTime - video.currentTime) > 0.12 && !reference.ended))
            reference.currentTime = video.currentTime;
        if (video.paused) reference.pause();
        else if (reference.paused && !reference.ended) reference.play().catch(() => {});
    };
    const display = () => {
        const active = comparing && Boolean(reference.src);
        reference.style.display = divider.style.display = active ? "block" : "none";
        reference.style.clipPath = `inset(0 ${(1 - wipe) * 100}% 0 0)`;
        divider.style.left = `${wipe * 100}%`;
        setOn(compare, active);
        stage.style.cursor = active ? "ew-resize" : "";
        stage.style.touchAction = active ? "none" : "";
        // Wipe moves only redraw the clip/divider; reference sync happens on toggle and seeks.
        if (!active) reference.pause();
    };
    const update = () => {
        const index = Math.min(frames - 1, Math.max(0, Math.round(video.currentTime * fps)));
        if (frames > 0) {
            if (!scrubbing) timeline.value = String(index);
            frame.textContent = `${index + 1} / ${frames}`;
        }
        sync();
    };
    const seek = (index) => {
        if (!frames) return;
        video.currentTime = Math.min(frames - 1, Math.max(0, index)) / fps;
        frame.textContent = `${Math.min(frames - 1, Math.max(0, index)) + 1} / ${frames}`;
        sync(true);
    };
    play.onclick = () => { if (video.paused) video.play().catch(() => {}); else video.pause(); };
    previous.onclick = () => { video.pause(); seek(Math.round(video.currentTime * fps) - 1); };
    next.onclick = () => { video.pause(); seek(Math.round(video.currentTime * fps) + 1); };
    timeline.addEventListener("pointerdown", () => { scrubbing = true; video.pause(); });
    timeline.addEventListener("change", () => { scrubbing = false; });
    timeline.oninput = () => seek(Number(timeline.value));
    compare.onclick = () => { comparing = !comparing; display(); if (comparing) sync(true); };
    loop.onclick = () => { video.loop = !video.loop; setOn(loop, video.loop); };
    mute.onclick = () => { video.muted = !video.muted; syncMute(); };
    volume.oninput = () => { video.volume = Number(volume.value); };
    full.onclick = () => {
        if (document.fullscreenElement) {
            document.exitFullscreen().catch((cause) => console.warn("[media_video_viewer] exit fullscreen rejected:", cause));
            return;
        }
        // Escape exits fullscreen natively; video keeps object-fit:contain inside the stage.
        const request = stage.requestFullscreen ? stage.requestFullscreen() : Promise.reject(new Error("Fullscreen API unavailable"));
        request.catch((cause) => console.warn("[media_video_viewer] fullscreen rejected:", cause));
    };
    // Wipe follows the pointer on hover, not only while dragging; updates clip/divider
    // only, never seeks. Viewport-space offset, so divide by the transformed rect width
    // (clientWidth ignores node zoom scaling).
    stage.addEventListener("pointermove", (event) => {
        if (!comparing || !reference.src) return;
        const rect = stage.getBoundingClientRect();
        wipe = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
        display();
    });
    video.addEventListener("timeupdate", update);
    video.addEventListener("seeked", update);
    video.addEventListener("play", () => { syncPlay(true); sync(true); });
    video.addEventListener("pause", () => { syncPlay(false); sync(); });
    reference.addEventListener("loadedmetadata", () => sync(true));
    video.addEventListener("loadedmetadata", () => {
        if (video.videoWidth && video.videoHeight) stage.style.aspectRatio = `${video.videoWidth} / ${video.videoHeight}`;
        sync(true);
        video.play().catch(() => {});
    });
    display();
    // LiteGraph button text color is unreliable in the Vue frontend; a DOM widget with
    // explicit colors stays legible. serialize:false keeps workflow values untouched.
    const browseHost = document.createElement("div");
    const browseButton = element("button", browseHost, {
        width: "100%", boxSizing: "border-box", padding: "5px 8px",
        color: "#111", background: "#6aa9ff", border: "1px solid #6aa9ff", borderRadius: "4px",
        font: "600 12px system-ui, sans-serif", cursor: "pointer",
    });
    browseButton.type = "button";
    browseButton.textContent = "Browse server output…";
    browseButton.onclick = () => { closeBrowser?.(); closeBrowser = browse(node); };
    // Pin to the button's height or distributeSpace hands the row all surplus node height.
    const browseH = () => 36;
    const browseWidget = node.addDOMWidget("media_video_browse", "button", browseHost, {
        serialize: false, getMinHeight: browseH, getMaxHeight: browseH, getHeight: browseH,
    });
    browseWidget.serialize = false;
    // Browse edits `subfolder`/`output_root`; put it directly above `subfolder`.
    const browseAt = node.widgets.indexOf(browseWidget);
    const subfolderAt = node.widgets.findIndex((w) => w.name === "subfolder");
    if (browseAt !== -1 && subfolderAt !== -1) {
        node.widgets.splice(browseAt, 1);
        node.widgets.splice(subfolderAt, 0, browseWidget);
    }
    const onRemoved = node.onRemoved;
    node.onRemoved = function () {
        cancelAnimationFrame(rafHandle);
        widthObserver.disconnect();
        closeBrowser?.();
        video.pause(); reference.pause();
        video.removeAttribute("src"); reference.removeAttribute("src");
        video.load(); reference.load();
        onRemoved?.apply(this, arguments);
    };
    const reset = () => {
        video.pause(); reference.pause();
        syncPlay(false);
        video.removeAttribute("src"); video.load();
        reference.removeAttribute("src"); reference.load();
        comparing = false;
        compare.disabled = play.disabled = previous.disabled = next.disabled = timeline.disabled = true;
        saved.textContent = "";
        saved.removeAttribute("title");
        download.removeAttribute("href");
        download.style.display = "none";
        empty.style.display = "grid";
        stage.style.aspectRatio = "16 / 9";
        frame.textContent = "–";
        display();
    };
    return (info) => {
        if (!info?.preview_url) return reset();
        video.pause(); reference.pause();
        fps = Number(info.fps) > 0 ? Number(info.fps) : 24;
        frames = Math.max(0, Number(info.frames) || 0);
        timeline.max = String(Math.max(0, frames - 1));
        timeline.value = "0";
        timeline.disabled = !frames;
        frame.textContent = frames ? `1 / ${frames}` : "–";
        play.disabled = previous.disabled = next.disabled = false;
        video.src = api.apiURL(info.preview_url);
        if (info.reference_url) reference.src = api.apiURL(info.reference_url);
        else {
            reference.removeAttribute("src"); reference.load();
            comparing = false;
        }
        compare.disabled = !info.reference_url;
        empty.style.display = "none";
        saved.textContent = info.saved_path ? `Saved to ${info.saved_path}` : "Preview only (ComfyUI temp)";
        saved.title = info.saved_path || "";
        if (info.download_url) {
            download.href = api.apiURL(info.download_url);
            // saved_path is None for preview-only; backend supplies the real basename.
            download.download = info.download_name || "";
            download.style.display = "grid";
        } else {
            download.removeAttribute("href");
            download.removeAttribute("download");
            download.style.display = "none";
        }
        display();
        requestAnimationFrame(() => node.setSize?.([
            Math.max(node.size[0], 360), Math.max(node.size[1], node.computeSize()[1]),
        ]));
    };
}

app.registerExtension({
    name: "media.MediaVideoViewer",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "MediaVideoViewer") return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            this.__mediaVideoViewer = install(this);
        };
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            onConfigure?.apply(this, arguments);
            // Preview tokens don't survive reload; reset the viewer and restore layout size.
            this.__mediaVideoViewer?.(null);
            this.__mediaVideoSync?.();
            requestAnimationFrame(() => this.setSize?.([
                Math.max(this.size[0], 360), Math.max(this.size[1], this.computeSize?.()[1] ?? this.size[1]),
            ]));
        };
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (msg) {
            onExecuted?.apply(this, arguments);
            this.__mediaVideoViewer?.(msg?.media_video?.[0]);
        };
    },
});
