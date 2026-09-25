// A fake After Effects object model for the tests: the subset aemcp.jsx uses, with After Effects' rules where
// they matter (new layers on top, setValue refused on animated properties, one temporal ease per dimension except
// for spatial properties, TextDocument values returned as copies, influence 0.1-100, undo groups).
"use strict";
const fs = require("fs");
const path = require("path");

const PropertyType = {PROPERTY: 6812, INDEXED_GROUP: 6813, NAMED_GROUP: 6814};
const PropertyValueType = {NO_VALUE: 6412, ThreeD_SPATIAL: 6413, ThreeD: 6414, TwoD_SPATIAL: 6415, TwoD: 6416,
    OneD: 6417, COLOR: 6418, CUSTOM_VALUE: 6419, MARKER: 6420, LAYER_INDEX: 6421, MASK_INDEX: 6422, SHAPE: 6423,
    TEXT_DOCUMENT: 6424};
const KeyframeInterpolationType = {LINEAR: 6612, BEZIER: 6613, HOLD: 6614};
const RQItemStatus = {WILL_CONTINUE: 3012, NEEDS_OUTPUT: 3013, UNQUEUED: 3014, QUEUED: 3015, RENDERING: 3016,
    USER_STOPPED: 3017, ERR_STOPPED: 3018, DONE: 3019};
const ParagraphJustification = {LEFT_JUSTIFY: 7413, RIGHT_JUSTIFY: 7414, CENTER_JUSTIFY: 7415};
const MaskMode = {NONE: 6812, ADD: 6813, SUBTRACT: 6814, INTERSECT: 6815, LIGHTEN: 6816, DARKEN: 6817, DIFFERENCE: 6818};
const BlendingMode = {NORMAL: 5212, DISSOLVE: 5213, DARKEN: 5214, MULTIPLY: 5215, COLOR_BURN: 5216, LINEAR_BURN: 5217,
    LIGHTEN: 5218, SCREEN: 5219, COLOR_DODGE: 5220, LINEAR_DODGE: 5221, ADD: 5222, OVERLAY: 5223, SOFT_LIGHT: 5224,
    HARD_LIGHT: 5225, DIFFERENCE: 5226, EXCLUSION: 5227, HUE: 5228, SATURATION: 5229, COLOR: 5230, LUMINOSITY: 5231};
const TrackMatteType = {NO_TRACK_MATTE: 5012, ALPHA: 5013, ALPHA_INVERTED: 5014, LUMA: 5015, LUMA_INVERTED: 5016};
// PNG signature + an empty IEND chunk: export_frame waits for IEND.
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82]);

const EFFECTS = [
    {displayName: "Gaussian Blur", matchName: "ADBE Gaussian Blur 2", category: "Blur & Sharpen",
        params: {"Blurriness": 0, "Blur Dimensions": 1, "Repeat Edge Pixels": 0}},
    {displayName: "Glow", matchName: "ADBE Glo2", category: "Stylize",
        params: {"Glow Threshold": 60, "Glow Radius": 10, "Glow Intensity": 1}},
    {displayName: "Fill", matchName: "ADBE Fill", category: "Generate", params: {"Color": [1, 0, 0, 1]}},
];

function makeAE(CtxArray) {
    // Arrays handed to the library are made with the script context's own Array, so `instanceof Array` holds there
    // as it does in After Effects' single realm; arrays coming from the library are read with plain loops.
    CtxArray = CtxArray || Array;
    let nextId = 1;
    const undo = [];
    const undoLog = [];

    function arr(v) {
        const out = new CtxArray();
        for (let i = 0; i < v.length; i++) {
            out.push(v[i]);
        }
        return out;
    }

    function clone(v) {
        return Array.isArray(v) ? arr(v) : v;
    }

    class KeyframeEase {
        constructor(speed, influence) {
            if (!(influence >= 0.1 && influence <= 100)) {
                throw new Error("KeyframeEase influence must be 0.1-100");
            }
            this.speed = speed;
            this.influence = influence;
        }
    }

    class Shape {
        constructor() {
            this.vertices = [];
            this.inTangents = [];
            this.outTangents = [];
            this.closed = true;
        }
    }

    class MarkerValue {
        constructor(comment) {
            this.comment = comment;
            this.duration = 0;
        }
    }

    function needUndo() {
        if (undo.length === 0) {
            throw new Error("fake AE: change outside an undo group");
        }
    }

    class TextDocument {
        constructor(text) {
            this.text = text;
            this.font = "ArialMT";
            this.fontSize = 72;
            this.fillColor = [1, 1, 1];
            this.applyFill = true;
            this.justification = ParagraphJustification.LEFT_JUSTIFY;
        }
        copy() {
            return Object.assign(new TextDocument(this.text), this, {fillColor: arr(this.fillColor)});
        }
    }

    class Property {
        constructor(name, matchName, value, valueType, spatial) {
            this.name = name;
            this.matchName = matchName;
            this._value = value;
            this.propertyType = PropertyType.PROPERTY;
            this.propertyValueType = valueType;
            this.isSpatial = !!spatial;
            this.keys = [];  // {time, value, inType, outType, inEase, outEase}
            this.expression = "";
            this.expressionEnabled = false;
            this.canSetExpression = valueType !== PropertyValueType.TEXT_DOCUMENT || true;
        }
        _check() {
            if (undo.length === 0) {
                throw new Error("fake AE: change outside an undo group");
            }
        }
        _v(v) {
            if (this.propertyValueType === PropertyValueType.SHAPE) {
                if (!(v instanceof Shape) || v.vertices.length < 2) {
                    throw new Error(this.name + " takes a Shape with at least two vertices");
                }
                return v;
            }
            if (this.propertyValueType === PropertyValueType.MARKER) {
                if (!(v instanceof MarkerValue)) {
                    throw new Error(this.name + " takes a MarkerValue");
                }
                return v;
            }
            if (this.propertyValueType === PropertyValueType.COLOR && (!Array.isArray(v) || v.length !== 4)) {
                throw new Error(this.name + " takes [r, g, b, a]");
            }
            if (this.propertyValueType === PropertyValueType.TEXT_DOCUMENT) {
                if (!(v instanceof TextDocument)) {
                    throw new Error("Source Text takes a TextDocument");
                }
                return v.copy();
            }
            if (Array.isArray(this._value)) {
                if (!Array.isArray(v) || v.length < 2 || v.length > this._value.length) {
                    throw new Error(this.name + " needs an array of " + this._value.length + " values");
                }
                const out = Array.prototype.slice.call(this._value);
                for (let i = 0; i < v.length; i++) {
                    if (typeof v[i] !== "number") {
                        throw new Error(this.name + " needs numbers");
                    }
                    out[i] = v[i];
                }
                return out;
            }
            if (typeof v !== "number") {
                throw new Error(this.name + " needs a number");
            }
            return v;
        }
        get value() {
            const v = this.keys.length ? this.keys[0].value : this._value;
            return v instanceof TextDocument ? v.copy() : clone(v);
        }
        get numKeys() {
            return this.keys.length;
        }
        setValue(v) {
            this._check();
            if (this.keys.length) {
                throw new Error("After Effects error: can not use setValue on a property with keyframes");
            }
            this._value = this._v(v);
        }
        setValueAtTime(t, v) {
            this._check();
            const val = this._v(v);
            const k = this.keys.find((x) => Math.abs(x.time - t) < 1e-6);
            if (k) {
                k.value = val;
            } else {
                this.keys.push({time: t, value: val, inType: KeyframeInterpolationType.LINEAR,
                    outType: KeyframeInterpolationType.LINEAR});
                this.keys.sort((a, b) => a.time - b.time);
            }
        }
        nearestKeyIndex(t) {
            let best = 1;
            this.keys.forEach((k, i) => {
                if (Math.abs(k.time - t) < Math.abs(this.keys[best - 1].time - t)) {
                    best = i + 1;
                }
            });
            return best;
        }
        keyTime(i) {
            return this.keys[i - 1].time;
        }
        keyValue(i) {
            const v = this.keys[i - 1].value;
            return v instanceof TextDocument ? v.copy() : clone(v);
        }
        removeKey(i) {
            this._check();
            this.keys.splice(i - 1, 1);
        }
        setInterpolationTypeAtKey(i, inType, outType) {
            this._check();
            Object.assign(this.keys[i - 1], {inType, outType});
        }
        setTemporalEaseAtKey(i, inEase, outEase) {
            this._check();
            const dims = (Array.isArray(this._value) && !this.isSpatial) ? this._value.length : 1;
            if (inEase.length !== dims || outEase.length !== dims) {
                throw new Error("After Effects error: " + this.name + " takes " + dims + " temporal ease(s) per side");
            }
            Object.assign(this.keys[i - 1], {inEase, outEase});
        }
        copy() {
            return Object.assign(Object.create(Object.getPrototypeOf(this)), this,
                {keys: this.keys.map((k) => Object.assign({}, k)), _value: clone(this._value)});
        }
        get expressionError() {
            if (!this.expressionEnabled) {
                return "";
            }
            let depth = 0;
            for (const c of this.expression) {
                depth += c === "(" ? 1 : c === ")" ? -1 : 0;
            }
            return depth === 0 ? "" : "Error: SyntaxError: missing ) in parenthetical";
        }
    }

    class PropertyGroup {
        constructor(name, matchName, children, type) {
            this.name = name;
            this.matchName = matchName;
            this.children = children;
            this.propertyType = type || PropertyType.NAMED_GROUP;
        }
        get numProperties() {
            return this.children.length;
        }
        property(ref) {
            if (typeof ref === "number") {
                return this.children[ref - 1] || null;
            }
            return this.children.find((c) => c.name === ref || c.matchName === ref) || null;
        }
        copy() {
            return Object.assign(Object.create(Object.getPrototypeOf(this)), this,
                {children: this.children.map((c) => c.copy())});
        }
    }

    // An indexed group that takes addProperty(matchName): masks, shape contents, text animators and selectors.
    // factories: {matchName: [display name, () => children array | a single Property, extra fields]}.
    class AddGroup extends PropertyGroup {
        constructor(name, matchName, factories) {
            super(name, matchName, [], PropertyType.INDEXED_GROUP);
            this.factories = factories;
        }
        canAddProperty(n) {
            return n in this.factories;
        }
        addProperty(n) {
            needUndo();
            if (!(n in this.factories)) {
                throw new Error("After Effects error: can not add property " + n + " to " + this.name);
            }
            const [display, build, extra] = this.factories[n];
            const made = build();
            let prop = made;
            if (Array.isArray(made)) {
                const count = this.children.filter((c) => c.matchName === n).length + 1;
                prop = Object.assign(new PropertyGroup(display + " " + count, n, made), extra ? extra() : {});
            }
            this.children.push(prop);
            return prop;
        }
    }

    const P = (name, match, value, type, spatial) => () => new Property(name, match, value, type, spatial);
    const SHAPE_CONTENTS = {
        "ADBE Vector Shape - Rect": ["Rectangle Path", () => [
            new Property("Size", "ADBE Vector Rect Size", [100, 100], PropertyValueType.TwoD),
            new Property("Position", "ADBE Vector Rect Position", [0, 0], PropertyValueType.TwoD_SPATIAL, true),
            new Property("Roundness", "ADBE Vector Rect Roundness", 0, PropertyValueType.OneD)]],
        "ADBE Vector Shape - Ellipse": ["Ellipse Path", () => [
            new Property("Size", "ADBE Vector Ellipse Size", [100, 100], PropertyValueType.TwoD),
            new Property("Position", "ADBE Vector Ellipse Position", [0, 0], PropertyValueType.TwoD_SPATIAL, true)]],
        "ADBE Vector Shape - Star": ["Polystar Path", () => [
            new Property("Type", "ADBE Vector Star Type", 1, PropertyValueType.OneD),
            new Property("Points", "ADBE Vector Star Points", 5, PropertyValueType.OneD),
            new Property("Outer Radius", "ADBE Vector Star Outer Radius", 50, PropertyValueType.OneD),
            new Property("Inner Radius", "ADBE Vector Star Inner Radius", 25, PropertyValueType.OneD)]],
        "ADBE Vector Graphic - Fill": ["Fill", () => [
            new Property("Color", "ADBE Vector Fill Color", [1, 0, 0, 1], PropertyValueType.COLOR),
            new Property("Opacity", "ADBE Vector Fill Opacity", 100, PropertyValueType.OneD)]],
        "ADBE Vector Graphic - Stroke": ["Stroke", () => [
            new Property("Color", "ADBE Vector Stroke Color", [1, 1, 1, 1], PropertyValueType.COLOR),
            new Property("Stroke Width", "ADBE Vector Stroke Width", 2, PropertyValueType.OneD)]],
    };
    const shapeRoot = () => new AddGroup("Contents", "ADBE Root Vectors Group", {
        "ADBE Vector Group": ["Group", () => [
            new AddGroup("Contents", "ADBE Vectors Group", SHAPE_CONTENTS),
            new PropertyGroup("Transform", "ADBE Vector Transform Group", [
                new Property("Position", "ADBE Vector Position", [0, 0], PropertyValueType.TwoD_SPATIAL, true),
                new Property("Scale", "ADBE Vector Scale", [100, 100], PropertyValueType.TwoD),
                new Property("Rotation", "ADBE Vector Rotation", 0, PropertyValueType.OneD),
                new Property("Opacity", "ADBE Vector Group Opacity", 100, PropertyValueType.OneD)])]],
    });
    const masks = () => new AddGroup("Masks", "ADBE Mask Parade", {
        "ADBE Mask Atom": ["Mask", () => [
            new Property("Mask Path", "ADBE Mask Shape", new Shape(), PropertyValueType.SHAPE),
            new Property("Mask Feather", "ADBE Mask Feather", [0, 0], PropertyValueType.TwoD),
            new Property("Mask Opacity", "ADBE Mask Opacity", 100, PropertyValueType.OneD),
            new Property("Mask Expansion", "ADBE Mask Offset", 0, PropertyValueType.OneD)],
        () => ({_mode: MaskMode.ADD, inverted: false,
            get maskMode() { return this._mode; },
            set maskMode(m) {
                if (!Object.values(MaskMode).includes(m)) {
                    throw new Error("After Effects error: bad mask mode " + m);
                }
                this._mode = m;
            }})],
    });
    const textAnimators = () => new AddGroup("Animators", "ADBE Text Animators", {
        "ADBE Text Animator": ["Animator", () => [
            new AddGroup("Selectors", "ADBE Text Selectors", {
                "ADBE Text Selector": ["Range Selector", () => [
                    new Property("Start", "ADBE Text Percent Start", 0, PropertyValueType.OneD),
                    new Property("End", "ADBE Text Percent End", 100, PropertyValueType.OneD),
                    new Property("Offset", "ADBE Text Percent Offset", 0, PropertyValueType.OneD),
                    new PropertyGroup("Advanced", "ADBE Text Range Advanced", [
                        new Property("Smoothness", "ADBE Text Selector Smoothness", 100, PropertyValueType.OneD)])]],
            }),
            new AddGroup("Properties", "ADBE Text Animator Properties", {
                "ADBE Text Opacity": ["Opacity", P("Opacity", "ADBE Text Opacity", 100, PropertyValueType.OneD)],
                "ADBE Text Position 3D": ["Position", P("Position", "ADBE Text Position 3D", [0, 0, 0],
                    PropertyValueType.ThreeD_SPATIAL, true)],
                "ADBE Text Scale 3D": ["Scale", P("Scale", "ADBE Text Scale 3D", [100, 100, 100], PropertyValueType.ThreeD)],
                "ADBE Text Blur": ["Blur", P("Blur", "ADBE Text Blur", [0, 0], PropertyValueType.TwoD)],
            })]],
    });

    class EffectParade extends PropertyGroup {
        constructor() {
            super("Effects", "ADBE Effect Parade", [], PropertyType.INDEXED_GROUP);
        }
        canAddProperty(n) {
            return EFFECTS.some((e) => e.displayName === n || e.matchName === n);
        }
        addProperty(n) {
            if (undo.length === 0) {
                throw new Error("fake AE: change outside an undo group");
            }
            const e = EFFECTS.find((x) => x.displayName === n || x.matchName === n);
            const same = this.children.filter((c) => c.matchName === e.matchName).length;
            const params = Object.keys(e.params).map((p) => new Property(p, e.matchName + "-" + p, clone(e.params[p]),
                Array.isArray(e.params[p]) ? PropertyValueType.COLOR : PropertyValueType.OneD));
            const fx = new PropertyGroup(same ? e.displayName + " " + (same + 1) : e.displayName, e.matchName, params);
            this.children.push(fx);
            return fx;
        }
    }

    class Item {
        constructor(name) {
            this.id = nextId++;
            this.name = name;
            this.parentFolder = null;
        }
    }
    class FolderItem extends Item {}
    class SolidSource {
        constructor(color) {
            this.color = color;
        }
    }
    class FootageItem extends Item {
        constructor(name, file, opts) {
            super(name);
            this.file = file;
            this.mainSource = opts.solid ? new SolidSource(opts.solid) : {};
            this.width = opts.width || 1920;
            this.height = opts.height || 1080;
            this.duration = opts.duration || 0;
            this.frameRate = opts.frameRate || 0;
        }
        replace(file) {
            needUndo();
            this.file = file;
            this.name = path.basename(file.fsName);
        }
        replaceWithSequence(file) {
            needUndo();
            this.file = file;
            this.name = path.basename(file.fsName).replace(/\d+(\.\w+)$/, "[####]$1");
        }
    }

    class AVLayer {
        constructor(comp, name, source) {
            this.containingComp = comp;
            this.name = name;
            this.source = source || null;
            this._start = 0;
            this.inPoint = 0;
            this.outPoint = comp.duration;
            this.enabled = true;
            this._parent = null;
            this.nullLayer = false;
            this.adjustmentLayer = false;
            const w = comp.width, h = comp.height;
            this.groups = [
                new PropertyGroup("Transform", "ADBE Transform Group", [
                    new Property("Anchor Point", "ADBE Anchor Point", [w / 2, h / 2, 0], PropertyValueType.ThreeD_SPATIAL, true),
                    new Property("Position", "ADBE Position", [w / 2, h / 2, 0], PropertyValueType.ThreeD_SPATIAL, true),
                    new Property("Scale", "ADBE Scale", [100, 100, 100], PropertyValueType.ThreeD),
                    new Property("Rotation", "ADBE Rotate Z", 0, PropertyValueType.OneD),
                    new Property("Opacity", "ADBE Opacity", 100, PropertyValueType.OneD),
                ]),
                new EffectParade(),
                masks(),
                new Property("Marker", "ADBE Marker", null, PropertyValueType.MARKER),
            ];
            Object.assign(this, {blendingMode: BlendingMode.NORMAL, threeDLayer: false, motionBlur: false, shy: false,
                solo: false, locked: false, trackMatteLayer: null, trackMatteType: TrackMatteType.NO_TRACK_MATTE});
        }
        get canSetTimeRemapEnabled() {
            const src = this.source;
            return !!src && (src instanceof CompItem || (src instanceof FootageItem && !src.mainSource.color &&
                src.duration > 0));
        }
        get timeRemapEnabled() {
            return !!this.property("ADBE Time Remapping");
        }
        set timeRemapEnabled(on) {
            needUndo();
            if (on && !this.canSetTimeRemapEnabled) {
                throw new Error("After Effects error: time remapping is not available for this layer");
            }
            this.groups = this.groups.filter((g) => g.matchName !== "ADBE Time Remapping");
            if (on) {
                // After Effects starts time remapping with keys at the in and out points
                const tr = new Property("Time Remap", "ADBE Time Remapping", 0, PropertyValueType.OneD);
                tr.keys.push({time: this.inPoint, value: 0, inType: 6612, outType: 6612},
                    {time: this.outPoint, value: this.source.duration, inType: 6612, outType: 6612});
                this.groups.unshift(tr);
            }
        }
        applyPreset(file) {
            needUndo();
            if (!file.exists) {
                throw new Error("After Effects error: preset file not found");
            }
            this.property("ADBE Effect Parade").addProperty("ADBE Glo2");  // the fake preset holds a Glow
        }
        setTrackMatte(layer, type) {
            needUndo();
            if (this.locked) {
                throw new Error("After Effects error: the layer is locked");
            }
            if (layer === this || layer.containingComp !== this.containingComp) {
                throw new Error("After Effects error: the track matte must be another layer of the same comp");
            }
            if (!Object.values(TrackMatteType).includes(type) || type === TrackMatteType.NO_TRACK_MATTE) {
                throw new Error("After Effects error: bad track matte type");
            }
            Object.assign(this, {trackMatteLayer: layer, trackMatteType: type});
        }
        removeTrackMatte() {
            Object.assign(this, {trackMatteLayer: null, trackMatteType: TrackMatteType.NO_TRACK_MATTE});
        }
        duplicate() {
            needUndo();
            const layers = this.containingComp._layers;
            const d = Object.assign(Object.create(Object.getPrototypeOf(this)), this,
                {groups: this.groups.map((g) => g.copy())});
            layers.splice(layers.indexOf(this), 0, d);  // the copy goes right above the original
            return d;
        }
        _move(to) {
            needUndo();
            const layers = this.containingComp._layers;
            layers.splice(layers.indexOf(this), 1);
            layers.splice(to(layers), 0, this);
        }
        moveToBeginning() { this._move(() => 0); }
        moveToEnd() { this._move((ls) => ls.length); }
        moveBefore(l) { this._move((ls) => ls.indexOf(l)); }
        moveAfter(l) { this._move((ls) => ls.indexOf(l) + 1); }
        get index() {
            return this.containingComp._layers.indexOf(this) + 1;
        }
        get startTime() {
            return this._start;
        }
        set startTime(t) {
            // moving a layer in time moves its in and out points with it
            const shift = t - this._start;
            this._start = t;
            this.inPoint += shift;
            this.outPoint += shift;
        }
        get parent() {
            return this._parent;
        }
        set parent(p) {
            if (p === this) {
                throw new Error("a layer cannot be its own parent");
            }
            this._parent = p;
        }
        property(ref) {
            if (typeof ref === "number") {
                return this.groups[ref - 1] || null;
            }
            return this.groups.find((g) => g.name === ref || g.matchName === ref) || null;
        }
        get numProperties() {
            return this.groups.length;
        }
        remove() {
            const layers = this.containingComp._layers;
            layers.splice(layers.indexOf(this), 1);
            layers.forEach((l) => { if (l._parent === this) { l._parent = null; } });
        }
    }
    class TextLayer extends AVLayer {
        constructor(comp, text) {
            super(comp, text);
            this.rect = {left: -150, top: -60, width: 300, height: 80};  // text grows up and right from its anchor
            this.groups.unshift(new PropertyGroup("Text", "ADBE Text Properties", [
                new Property("Source Text", "ADBE Text Document", new TextDocument(text), PropertyValueType.TEXT_DOCUMENT),
                textAnimators(),
            ]));
        }
    }
    TextLayer.prototype.sourceRectAtTime = function () {
        return this.rect;
    };
    class ShapeLayer extends AVLayer {
        constructor(comp, name) {
            super(comp, name);
            this.groups.unshift(shapeRoot());
        }
        sourceRectAtTime() {
            return {left: -100, top: -100, width: 200, height: 200};
        }
    }
    const NOT_ON_3D = ["ADBE Effect Parade", "ADBE Mask Parade"];
    class CameraLayer extends AVLayer {
        constructor(comp, name) {
            super(comp, name);
            this.groups = this.groups.filter((g) => !NOT_ON_3D.includes(g.matchName));
        }
    }
    class LightLayer extends AVLayer {
        constructor(comp, name) {
            super(comp, name);
            this.groups = this.groups.filter((g) => !NOT_ON_3D.includes(g.matchName));
        }
    }

    class LayerCollection {
        constructor(comp) {
            this.comp = comp;
        }
        _top(l) {
            if (undo.length === 0) {
                throw new Error("fake AE: change outside an undo group");
            }
            this.comp._layers.unshift(l);  // new layers go on top: index 1
            return l;
        }
        addText(text) {
            return this._top(new TextLayer(this.comp, text));
        }
        addSolid(color, name, w, h, pa, dur) {
            const src = new FootageItem(name, null, {solid: color, width: w, height: h, duration: dur});
            project._items.push(src);
            return this._top(new AVLayer(this.comp, name, src));
        }
        addShape() {
            return this._top(new ShapeLayer(this.comp, "Shape Layer " + (this.comp._layers.length + 1)));
        }
        addNull(dur) {
            const l = new AVLayer(this.comp, "Null " + (this.comp._layers.length + 1));
            l.nullLayer = true;
            return this._top(l);
        }
        addCamera(name) {
            return this._top(new CameraLayer(this.comp, name));
        }
        addLight(name) {
            return this._top(new LightLayer(this.comp, name));
        }
        add(item) {
            return this._top(new AVLayer(this.comp, item.name, item));
        }
        precompose(indices, name, moveAll) {
            needUndo();
            const layers = this.comp._layers;
            const idx = Array.prototype.slice.call(indices).sort((a, b) => a - b);
            if (!idx.length || idx.some((i) => i < 1 || i > layers.length)) {
                throw new Error("After Effects error: bad layer indices for precompose");
            }
            if (!moveAll && idx.length > 1) {
                throw new Error("After Effects error: leaving attributes is only possible with one layer");
            }
            const c = this.comp;
            const nc = project.items.addComp(name, c.width, c.height, c.pixelAspect, c.duration, c.frameRate);
            const moving = idx.map((i) => layers[i - 1]);
            moving.forEach((l) => {
                layers.splice(layers.indexOf(l), 1);
                l.containingComp = nc;
                nc._layers.push(l);
            });
            layers.splice(idx[0] - 1, 0, new AVLayer(c, name, nc));
            return nc;
        }
    }

    class CompItem extends Item {
        constructor(name, w, h, pa, dur, fps) {
            super(name);
            Object.assign(this, {width: w, height: h, pixelAspect: pa, duration: dur, frameRate: fps});
            this.bgColor = [0, 0, 0];
            this._layers = [];
            this.layers = new LayerCollection(this);
            this.markerProperty = new Property("Marker", "ADBE Marker", null, PropertyValueType.MARKER);
            this._wa = [0, dur];
        }
        get workAreaStart() { return this._wa[0]; }
        set workAreaStart(v) {
            if (v < 0 || v >= this.duration) {
                throw new Error("After Effects error: work area start outside the comp");
            }
            this._wa[0] = v;
        }
        get workAreaDuration() { return this._wa[1]; }
        set workAreaDuration(v) {
            if (v <= 0 || this._wa[0] + v > this.duration + 1e-9) {
                throw new Error("After Effects error: work area outside the comp");
            }
            this._wa[1] = v;
        }
        get numLayers() {
            return this._layers.length;
        }
        layer(i) {
            return this._layers[i - 1];
        }
        openInViewer() {
            project.activeItem = this;
        }
        saveFrameToPng(t, file) {
            if (t < 0 || t > this.duration) {
                throw new Error("time " + t + " is outside the comp");
            }
            fs.writeFileSync(file.fsName, PNG);
        }
    }

    class File {
        constructor(p) {
            this.fsName = p;
            this.encoding = "BINARY";
        }
        get exists() {
            return fs.existsSync(this.fsName);
        }
        get name() {
            return path.basename(this.fsName);
        }
    }

    class ImportOptions {
        constructor(file) {
            this.file = file;
            this.sequence = false;
        }
    }

    const TEMPLATES = ["High Quality", "H.264 - Match Render Settings - 15 Mbps", "Lossless"];

    class RQItem {
        constructor(comp) {
            this.comp = comp;
            this.status = RQItemStatus.QUEUED;
            this.templates = arr(["Best Settings", "Draft Settings", "Multi-Machine Settings"]);
            const om = {file: null, template: "High Quality", templates: arr(TEMPLATES), applyTemplate(n) {
                if (!TEMPLATES.includes(n)) {
                    throw new Error("After Effects error: no output module template named " + n);
                }
                this.template = n;
            }};
            this._om = om;
        }
        outputModule(i) {
            return i === 1 ? this._om : null;
        }
        remove() {
            renderQueue._items.splice(renderQueue._items.indexOf(this), 1);
        }
    }

    const renderQueue = {
        _items: [],
        items: {add(comp) { const r = new RQItem(comp); renderQueue._items.push(r); return r; }},
        get numItems() { return this._items.length; },
        item(i) { return this._items[i - 1]; },
        render() {
            this._items.forEach((r) => {
                if (r.status === RQItemStatus.QUEUED) {
                    fs.writeFileSync(r._om.file.fsName, "rendered " + r.comp.name);
                    r.status = RQItemStatus.DONE;
                }
            });
        },
    };

    const root = new FolderItem("Root");
    nextId = 1;  // the root folder is not a numbered project item
    const project = {
        _items: [],
        rootFolder: root,
        activeItem: null,
        file: null,
        renderQueue,
        get numItems() { return this._items.length; },
        item(i) { return this._items[i - 1]; },
        items: {
            addComp(name, w, h, pa, dur, fps) {
                const c = new CompItem(name, w, h, pa, dur, fps);
                c.parentFolder = root;
                project._items.push(c);
                return c;
            },
            addFolder(name) {
                const f = new FolderItem(name);
                f.parentFolder = root;
                project._items.push(f);
                return f;
            },
        },
        importFile(opts) {
            const base = path.basename(opts.file.fsName);
            const name = opts.sequence ? base.replace(/\d+(\.\w+)$/, "[####]$1") : base;
            const it = new FootageItem(name, opts.file, {duration: opts.sequence ? 2 : 5, frameRate: 25});
            it.parentFolder = root;
            project._items.push(it);
            return it;
        },
        _uses() {
            const used = new Set();
            this._items.forEach((it) => {
                if (it instanceof CompItem) {
                    it._layers.forEach((l) => l.source && used.add(l.source));
                }
            });
            return used;
        },
        removeUnusedFootage() {
            needUndo();
            const used = this._uses();
            const gone = this._items.filter((it) => it instanceof FootageItem && !used.has(it));
            this._items = this._items.filter((it) => !gone.includes(it));
            return gone.length;
        },
        consolidateFootage() {
            needUndo();
            const keep = new Map(), gone = [];
            this._items.forEach((it) => {
                if (!(it instanceof FootageItem) || !it.file) {
                    return;
                }
                const k = it.file.fsName;
                if (keep.has(k)) {
                    gone.push([it, keep.get(k)]);
                } else {
                    keep.set(k, it);
                }
            });
            gone.forEach(([dup, kept]) => this._items.forEach((c) => {
                if (c instanceof CompItem) {
                    c._layers.forEach((l) => { if (l.source === dup) { l.source = kept; } });
                }
            }));
            this._items = this._items.filter((it) => !gone.some(([dup]) => dup === it));
            return gone.length;
        },
        save(file) {
            if (file) {
                this.file = file;
            }
            fs.writeFileSync(this.file.fsName, "aep");
        },
    };

    const app = {
        version: "26.0x10",
        project,
        effects: arr(EFFECTS.map((e) => ({displayName: e.displayName, matchName: e.matchName, category: e.category}))),
        beginUndoGroup(name) {
            undo.push(name);
            undoLog.push(name);
        },
        endUndoGroup() {
            if (!undo.length) {
                throw new Error("endUndoGroup without beginUndoGroup");
            }
            undo.pop();
        },
    };

    return {
        app, undo, undoLog,
        globals: {app, CompItem, FolderItem, FootageItem, SolidSource, TextLayer, ShapeLayer, CameraLayer, LightLayer,
            PropertyType, PropertyValueType, KeyframeInterpolationType, KeyframeEase, RQItemStatus,
            ParagraphJustification, File, ImportOptions, MaskMode, BlendingMode, TrackMatteType, Shape, MarkerValue},
    };
}

module.exports = {makeAE};
