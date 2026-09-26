// A fake After Effects object model for the tests: the subset aemcp.jsx uses, with After Effects' rules where
// they matter (new layers on top, setValue refused on animated properties, one temporal ease per dimension except
// for spatial properties, TextDocument values returned as copies, influence 0.1-100, undo groups).
"use strict";
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");

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
const ImportAsType = {COMP_CROPPED_LAYERS: 3812, FOOTAGE: 3813, COMP: 3814, PROJECT: 3815};
const LightType = {PARALLEL: 4412, SPOT: 4413, POINT: 4414, AMBIENT: 4415};
const TrackMatteType = {NO_TRACK_MATTE: 5012, ALPHA: 5013, ALPHA_INVERTED: 5014, LUMA: 5015, LUMA_INVERTED: 5016};
const AutoOrientType = {NO_AUTO_ORIENT: 4212, ALONG_PATH: 4213, CAMERA_OR_POINT_OF_INTEREST: 4214,
    CHARACTERS_TOWARD_CAMERA: 4215};
const RENDERERS = ["ADBE Advanced 3d", "ADBE Calder", "ADBE Ernst"];

// Footage whose file name starts with "moving" shows a checkered 30 px square on gray, its center at
// (100 + 60 t, 150 + 20 t) source pixels at t seconds into the layer: what track_point follows in the tests.
function movingSquare(x, y, t) {
    const cx = 100 + 60 * t, cy = 150 + 20 * t;
    if (Math.abs(x - cx) > 15 || Math.abs(y - cy) > 15) {
        return 40;
    }
    return (Math.floor((x - cx + 15) / 6) + Math.floor((y - cy + 15) / 6)) % 2 ? 230 : 90;
}

// An 8-bit RGB PNG, each row with the next of the five PNG filters so the decoder meets all of them.
function encodePng(w, h, gray) {
    const rows = [];
    let prev = Buffer.alloc(w * 3);
    for (let y = 0; y < h; y++) {
        const line = Buffer.alloc(w * 3);
        for (let x = 0; x < w; x++) {
            line.fill(gray(x, y), x * 3, x * 3 + 3);
        }
        const f = y % 5, out = Buffer.alloc(w * 3 + 1);
        out[0] = f;
        for (let i = 0; i < w * 3; i++) {
            const a = i >= 3 ? line[i - 3] : 0, b = prev[i], c = i >= 3 ? prev[i - 3] : 0;
            const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
            const pred = [0, a, b, (a + b) >> 1, pa <= pb && pa <= pc ? a : pb <= pc ? b : c][f];
            out[i + 1] = (line[i] - pred) & 255;
        }
        rows.push(out);
        prev = line;
    }
    const chunk = (kind, data) => {
        const len = Buffer.alloc(4), crc = Buffer.alloc(4);
        len.writeUInt32BE(data.length);
        crc.writeUInt32BE(zlib.crc32(Buffer.concat([Buffer.from(kind), data])));
        return Buffer.concat([len, Buffer.from(kind), data, crc]);
    };
    const ihdr = Buffer.alloc(13);
    ihdr.writeUInt32BE(w, 0);
    ihdr.writeUInt32BE(h, 4);
    ihdr.set([8, 2, 0, 0, 0], 8);
    return Buffer.concat([PNG.subarray(0, 8), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(Buffer.concat(rows))),
        chunk("IEND", Buffer.alloc(0))]);
}
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
            Object.assign(this, {tracking: 0, autoLeading: true, leading: 72, applyStroke: false,
                strokeColor: [0, 0, 0], strokeWidth: 0, allCaps: false, boxText: false});
        }
        copy() {
            return Object.assign(new TextDocument(this.text), this, {fillColor: arr(this.fillColor),
                strokeColor: arr(this.strokeColor)});
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
        valueAtTime(t, preExpression) {
            let v;
            if (!this.keys.length) {
                v = this.value;
            } else {
                const before = this.keys.filter((k) => k.time <= t + 1e-9);
                v = clone((before.length ? before[before.length - 1] : this.keys[0]).value);
            }
            if (this.expressionEnabled && this.expression && preExpression === false) {
                // the fake evaluates every expression as "value + time"
                v = Array.isArray(v) ? arr(Array.prototype.map.call(v, (x) => x + t)) : v + t;
            }
            return v;
        }
        setValuesAtTimes(times, values) {
            if (times.length !== values.length) {
                throw new Error("After Effects error: times and values differ in length");
            }
            for (let i = 0; i < times.length; i++) {
                this.setValueAtTime(times[i], values[i]);
            }
        }
        keyInInterpolationType(i) { return this.keys[i - 1].inType; }
        keyOutInterpolationType(i) { return this.keys[i - 1].outType; }
        _eases(i, side) {
            const dims = (Array.isArray(this._value) && !this.isSpatial) ? this._value.length : 1;
            return this.keys[i - 1][side] || arr(Array.from({length: dims}, () => new KeyframeEase(0, 16.67)));
        }
        keyInTemporalEase(i) { return this._eases(i, "inEase"); }
        keyOutTemporalEase(i) { return this._eases(i, "outEase"); }
        addToMotionGraphicsTemplateAs(comp, name) {
            if (this.propertyValueType === PropertyValueType.NO_VALUE) {
                return false;
            }
            comp._mogrt.push([this.name, name]);
            return true;
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
        _spatial(i) {
            this._check();
            if (!this.isSpatial) {
                throw new Error("After Effects error: " + this.name + " is not a spatial property");
            }
            return this.keys[i - 1];
        }
        setSpatialAutoBezierAtKey(i, on) { this._spatial(i).spatialAuto = on; }
        setSpatialContinuousAtKey(i, on) { this._spatial(i).spatialContinuous = on; }
        setSpatialTangentsAtKey(i, inT, outT) {
            if (inT.length !== this._value.length || outT.length !== this._value.length) {
                throw new Error("After Effects error: tangents need " + this._value.length + " values");
            }
            this._spatial(i).tangents = [Array.from(inT), Array.from(outT)];
        }
        setRovingAtKey(i, on) {
            // only inner keys of a spatial property can rove
            if (i === 1 || i === this.keys.length) {
                throw new Error("After Effects error: the first and last keyframes cannot rove");
            }
            this._spatial(i).roving = on;
        }
        setTemporalContinuousAtKey(i, on) { this._check(); this.keys[i - 1].temporalContinuous = on; }
        setTemporalAutoBezierAtKey(i, on) {
            this._check();
            if (this.keys[i - 1].inType !== KeyframeInterpolationType.BEZIER) {
                throw new Error("After Effects error: temporal auto Bezier needs Bezier keyframes");
            }
            this.keys[i - 1].temporalAuto = on;
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
            const count = this.children.filter((c) => c.matchName === n).length + 1;
            let prop = made;
            if (Array.isArray(made)) {
                prop = Object.assign(new PropertyGroup(display + " " + count, n, made), extra ? extra() : {});
            } else if (made instanceof AddGroup) {
                made.name = display + " " + count;
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
        "ADBE Vector Filter - Trim": ["Trim Paths", () => [
            new Property("Start", "ADBE Vector Trim Start", 0, PropertyValueType.OneD),
            new Property("End", "ADBE Vector Trim End", 100, PropertyValueType.OneD),
            new Property("Offset", "ADBE Vector Trim Offset", 0, PropertyValueType.OneD)]],
        "ADBE Vector Filter - Repeater": ["Repeater", () => [
            new Property("Copies", "ADBE Vector Repeater Copies", 3, PropertyValueType.OneD),
            new Property("Offset", "ADBE Vector Repeater Offset", 0, PropertyValueType.OneD),
            new PropertyGroup("Transform", "ADBE Vector Repeater Transform", [
                new Property("Position", "ADBE Vector Repeater Position", [100, 0], PropertyValueType.TwoD_SPATIAL, true),
                new Property("Scale", "ADBE Vector Repeater Scale", [100, 100], PropertyValueType.TwoD),
                new Property("Rotation", "ADBE Vector Repeater Rotation", 0, PropertyValueType.OneD),
                new Property("Start Opacity", "ADBE Vector Repeater Opacity 1", 100, PropertyValueType.OneD),
                new Property("End Opacity", "ADBE Vector Repeater Opacity 2", 100, PropertyValueType.OneD)])]],
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

    const trackers = () => new AddGroup("Motion Trackers", "ADBE MTrackers", {
        "ADBE MTracker": ["Tracker", () => new AddGroup(null, "ADBE MTracker", {
            "ADBE MTracker Pt": ["Track Point", () => [
                new Property("Feature Center", "ADBE MTracker Pt Feature Center", [0, 0], PropertyValueType.TwoD_SPATIAL,
                    true),
                new Property("Feature Size", "ADBE MTracker Pt Feature Size", [20, 20], PropertyValueType.TwoD),
                new Property("Search Size", "ADBE MTracker Pt Search Size", [40, 40], PropertyValueType.TwoD),
                new Property("Confidence", "ADBE MTracker Pt Confidence", 0, PropertyValueType.OneD),
                new Property("Attach Point", "ADBE MTracker Pt Attach Pt", [0, 0], PropertyValueType.TwoD_SPATIAL,
                    true)]],
        })],
    });
    const geometry = () => new PropertyGroup("Geometry Options", "ADBE Extrsn Options Group", [
        new Property("Bevel Style", "ADBE Bevel Styles", 1, PropertyValueType.OneD),
        new Property("Bevel Depth", "ADBE Bevel Depth", 2, PropertyValueType.OneD),
        new Property("Extrusion Depth", "ADBE Extrsn Depth", 0, PropertyValueType.OneD)]);
    const material = () => new PropertyGroup("Material Options", "ADBE Material Options Group", [
        ["Casts Shadows", "ADBE Casts Shadows", 0], ["Accepts Shadows", "ADBE Accepts Shadows", 1],
        ["Accepts Lights", "ADBE Accepts Lights", 1], ["Specular Intensity", "ADBE Specular Coefficient", 50],
        ["Specular Shininess", "ADBE Shininess Coefficient", 5], ["Metal", "ADBE Metal Coefficient", 100],
        ["Reflection Intensity", "ADBE Reflection Coefficient", 0],
    ].map(([n, m, v]) => new Property(n, m, v, PropertyValueType.OneD)));

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
            this.hasAudio = !!file && /\.(wav|mp3|aif|aiff|m4a|mov|mp4)$/i.test(file.fsName);
        }
        get footageMissing() {
            return !!this.file && !fs.existsSync(this.file.fsName);
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
                    new Property("X Rotation", "ADBE Rotate X", 0, PropertyValueType.OneD),
                    new Property("Y Rotation", "ADBE Rotate Y", 0, PropertyValueType.OneD),
                    new Property("Rotation", "ADBE Rotate Z", 0, PropertyValueType.OneD),
                    new Property("Opacity", "ADBE Opacity", 100, PropertyValueType.OneD),
                ]),
                new EffectParade(),
                masks(),
                new Property("Marker", "ADBE Marker", null, PropertyValueType.MARKER),
                trackers(),
                geometry(),
                material(),
            ];
            if (source && source.hasAudio) {
                this.groups.push(new PropertyGroup("Audio", "ADBE Audio Group", [
                    new Property("Audio Levels", "ADBE Audio Levels", [0, 0], PropertyValueType.TwoD)]));
            }
            this.selected = false;
            this.autoOrient = AutoOrientType.NO_AUTO_ORIENT;
            this.stretch = 100;
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
                // Removing the last key turns time remapping off, and the property then refuses values
                const layer = this, removeKey = tr.removeKey, setValueAtTime = tr.setValueAtTime;
                tr.removeKey = function (i) {
                    removeKey.call(this, i);
                    if (!this.keys.length) {
                        layer.groups = layer.groups.filter((g) => g !== tr);
                    }
                };
                tr.setValueAtTime = function (t, v) {
                    if (!layer.groups.includes(tr)) {
                        throw new Error("After Effects error: Can not “set value at time” with this property, " +
                            "because the property or a parent property is hidden.");
                    }
                    setValueAtTime.call(this, t, v);
                };
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
        get hasAudio() {
            return !!this.property("ADBE Audio Group");
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
    const NOT_ON_3D = ["ADBE Effect Parade", "ADBE Mask Parade", "ADBE MTrackers", "ADBE Extrsn Options Group",
        "ADBE Material Options Group"];
    class CameraLayer extends AVLayer {
        constructor(comp, name, center) {
            super(comp, name);
            this.groups = this.groups.filter((g) => !NOT_ON_3D.includes(g.matchName));
            // a camera's anchor point is its point of interest, at the given center; like After Effects 26 the
            // camera itself starts at [0, 0, -zoom] whatever the center
            const t = this.groups[0].children;
            t[0]._value = [center[0], center[1], 0];
            t[1]._value = [0, 0, -1777.8];
            this.groups.push(new PropertyGroup("Camera Options", "ADBE Camera Options Group", [
                new Property("Zoom", "ADBE Camera Zoom", 1777.8, PropertyValueType.OneD),
                new Property("Depth of Field", "ADBE Camera Depth of Field", 0, PropertyValueType.OneD),
                new Property("Focus Distance", "ADBE Camera Focus Distance", 1777.8, PropertyValueType.OneD),
                new Property("Aperture", "ADBE Camera Aperture", 25.3, PropertyValueType.OneD),
                new Property("Blur Level", "ADBE Camera Blur Level", 100, PropertyValueType.OneD)]));
        }
    }
    class LightLayer extends AVLayer {
        constructor(comp, name) {
            super(comp, name);
            this.groups = this.groups.filter((g) => !NOT_ON_3D.includes(g.matchName));
            this._type = LightType.SPOT;
            this._options();
        }
        get lightType() { return this._type; }
        set lightType(t) {
            if (!Object.values(LightType).includes(t)) {
                throw new Error("After Effects error: bad light type");
            }
            this._type = t;
            this._options();
        }
        _options() {
            // ambient lights have no shadows; only spots have a cone
            const opts = [new Property("Intensity", "ADBE Light Intensity", 100, PropertyValueType.OneD),
                new Property("Color", "ADBE Light Color", [1, 1, 1, 1], PropertyValueType.COLOR)];
            if (this._type === LightType.SPOT) {
                opts.push(new Property("Cone Angle", "ADBE Light Cone Angle", 90, PropertyValueType.OneD),
                    new Property("Cone Feather", "ADBE Light Cone Feather 2", 50, PropertyValueType.OneD));
            }
            if (this._type !== LightType.AMBIENT) {
                opts.push(new Property("Casts Shadows", "ADBE Casts Shadows", 0, PropertyValueType.OneD));
            }
            this.groups = this.groups.filter((g) => g.matchName !== "ADBE Light Options Group");
            this.groups.push(new PropertyGroup("Light Options", "ADBE Light Options Group", opts));
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
        addBoxText(size, text) {
            if (!Array.isArray(size) || size.length !== 2 || size[0] <= 0 || size[1] <= 0) {
                throw new Error("After Effects error: addBoxText needs [width, height]");
            }
            const l = new TextLayer(this.comp, text);
            const doc = l.groups[0].children[0]._value;
            Object.assign(doc, {boxText: true, boxTextSize: arr(size)});
            return this._top(l);
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
        addCamera(name, center) {
            return this._top(new CameraLayer(this.comp, name, center));
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
            this._mogrt = [];
            Object.assign(this, {motionBlur: false, shutterAngle: 180, shutterPhase: -90});
            this.motionGraphicsTemplateName = name;
            this._wa = [0, dur];
            this._renderer = RENDERERS[0];
            Object.assign(this, {motionBlur: false, shutterAngle: 180, shutterPhase: -90});
        }
        get renderers() { return arr(RENDERERS); }
        get renderer() { return this._renderer; }
        set renderer(r) {
            needUndo();
            if (!RENDERERS.includes(r)) {
                throw new Error("After Effects error: unknown renderer " + r);
            }
            this._renderer = r;
        }
        get frameDuration() {
            return 1 / this.frameRate;
        }
        remove() {
            needUndo();
            project._items.splice(project._items.indexOf(this), 1);
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
        get frameDuration() {
            return 1 / this.frameRate;
        }
        duplicate() {
            needUndo();
            const d = project.items.addComp(this.name + " 2", this.width, this.height, this.pixelAspect, this.duration,
                this.frameRate);
            d._layers = this._layers.map((l) => Object.assign(Object.create(Object.getPrototypeOf(l)), l,
                {groups: l.groups.map((g) => g.copy()), containingComp: d}));
            d._layers.forEach((l) => {
                if (l._parent) {
                    l._parent = d._layers[this._layers.indexOf(l._parent)];
                }
            });
            return d;
        }
        layer(i) {
            return this._layers[i - 1];
        }
        openInViewer() {
            project.activeItem = this;
        }
        exportAsMotionGraphicsTemplate(overwrite, p) {
            if (!this._mogrt.length || (!overwrite && fs.existsSync(p))) {
                return false;
            }
            fs.writeFileSync(p, JSON.stringify({name: this.motionGraphicsTemplateName, props: this._mogrt}));
            return true;
        }
        saveFrameToPng(t, file) {
            if (t < 0 || t > this.duration) {
                throw new Error("time " + t + " is outside the comp");
            }
            const l = this._layers.find((x) => x.source && x.source.file && /^moving/.test(x.source.file.name));
            if (!l) {
                fs.writeFileSync(file.fsName, PNG);
                return;
            }
            // the layer at position p with anchor a shows source pixel (x - p + a); 4 x 4 samples per pixel
            const tg = l.property("ADBE Transform Group"), p = tg.property("ADBE Position").value,
                a = tg.property("ADBE Anchor Point").value, st = t - l.startTime;
            fs.writeFileSync(file.fsName, encodePng(this.width, this.height, (x, y) => {
                let sum = 0;
                for (let i = 0; i < 16; i++) {
                    sum += movingSquare(x + (i % 4 + 0.5) / 4 - p[0] + a[0], y + (Math.floor(i / 4) + 0.5) / 4 - p[1] + a[1],
                        st);
                }
                return Math.round(sum / 16);
            }));
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
            this.importAs = ImportAsType.FOOTAGE;
        }
        canImportAs(t) {
            return t === ImportAsType.FOOTAGE || /\.(psd|ai)$/i.test(this.file.fsName);
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
            if (opts.importAs === ImportAsType.COMP || opts.importAs === ImportAsType.COMP_CROPPED_LAYERS) {
                // a layered file: a folder of layer footage and a comp holding them
                const stem = base.replace(/\.\w+$/, "");
                const folder = project.items.addFolder(stem + " Layers");
                const comp = project.items.addComp(stem, 1920, 1080, 1, 5, 25);
                ["Background", "Logo"].forEach((n) => {
                    const layerItem = new FootageItem(n + "/" + base, opts.file, {duration: 0});
                    layerItem.parentFolder = folder;
                    project._items.push(layerItem);
                    comp._layers.push(new AVLayer(comp, n, layerItem));
                });
                return comp;
            }
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
        reduceProject(comps) {
            needUndo();
            const keep = new Set();
            const visit = (it) => {
                if (keep.has(it)) {
                    return;
                }
                keep.add(it);
                if (it instanceof CompItem) {
                    it._layers.forEach((l) => l.source && visit(l.source));
                }
            };
            Array.prototype.forEach.call(comps, visit);
            keep.forEach((it) => { for (let f = it.parentFolder; f && f !== root; f = f.parentFolder) { keep.add(f); } });
            const before = this._items.length;
            this._items = this._items.filter((it) => keep.has(it));
            return before - this._items.length;
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
        findMenuCommandId(name) {
            return name === "Convert Audio to Keyframes" ? 5015 : 0;
        },
        executeCommand(id) {
            needUndo();
            const comp = project.activeItem;
            const sel = comp ? comp._layers.filter((l) => l.selected) : [];
            if (id !== 5015 || sel.length !== 1 || !sel[0].hasAudio) {
                return;  // like the menu: nothing happens without one selected layer with audio
            }
            const amp = new AVLayer(comp, "Audio Amplitude");
            amp.nullLayer = true;
            const slider = (n) => new PropertyGroup(n, "ADBE Slider Control", [
                new Property("Slider", "ADBE Slider Control-0001", 0, PropertyValueType.OneD)]);
            amp.property("ADBE Effect Parade").children.push(slider("Left Channel"), slider("Right Channel"),
                slider("Both Channels"));
            comp._layers.unshift(amp);
        },
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
            ParagraphJustification, File, ImportOptions, MaskMode, BlendingMode, TrackMatteType, Shape, MarkerValue,
            LightType, ImportAsType, AutoOrientType},
    };
}

module.exports = {makeAE};
