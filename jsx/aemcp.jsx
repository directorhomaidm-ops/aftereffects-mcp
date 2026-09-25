// aemcp: the After Effects side of aftereffects-mcp.
//
// ExtendScript is ECMAScript 3: no JSON object, no Array.prototype.map/forEach/indexOf, no String.prototype.trim,
// no let/const/arrow functions. Everything here sticks to that. The Python server appends one line,
// aemcp.run("command", {args}), and reads back the JSON string it returns.

var aemcp = (function () {
    // --- JSON out (arguments come in as JavaScript literals, so no parser is needed) ---

    function quote(s) {
        var out = '"', i, c, code, hex;
        for (i = 0; i < s.length; i++) {
            c = s.charAt(i);
            code = s.charCodeAt(i);
            if (c === '"' || c === "\\") {
                out += "\\" + c;
            } else if (code < 32 || code > 126) {
                // non-ASCII as \uXXXX: the result crosses AppleScript and a shell, keep it 7-bit
                hex = code.toString(16);
                while (hex.length < 4) {
                    hex = "0" + hex;
                }
                out += "\\u" + hex;
            } else {
                out += c;
            }
        }
        return out + '"';
    }

    function stringify(v) {
        var parts, k, i;
        if (v === null || v === undefined) {
            return "null";
        }
        if (typeof v === "number") {
            return isFinite(v) ? String(v) : "null";
        }
        if (typeof v === "boolean") {
            return v ? "true" : "false";
        }
        if (typeof v === "string") {
            return quote(v);
        }
        if (v instanceof Array) {
            parts = [];
            for (i = 0; i < v.length; i++) {
                parts.push(stringify(v[i]));
            }
            return "[" + parts.join(",") + "]";
        }
        parts = [];
        for (k in v) {
            if (v.hasOwnProperty(k) && typeof v[k] !== "function") {
                parts.push(quote(k) + ":" + stringify(v[k]));
            }
        }
        return "{" + parts.join(",") + "}";
    }

    function fail(msg) {
        throw new Error(msg);
    }

    function round(x, n) {
        var f = Math.pow(10, n === undefined ? 4 : n);
        return Math.round(x * f) / f;
    }

    function plain(v) {
        // property values to JSON-able values
        var out, i;
        if (v === null || v === undefined) {
            return null;
        }
        if (typeof v === "number") {
            return round(v, 4);
        }
        if (typeof v === "string" || typeof v === "boolean") {
            return v;
        }
        if (v instanceof Array) {
            out = [];
            for (i = 0; i < v.length; i++) {
                out.push(plain(v[i]));
            }
            return out;
        }
        if (v.text !== undefined && v.fontSize !== undefined) {
            out = {text: v.text, font: v.font, fontSize: v.fontSize, fillColor: plain(v.fillColor)};
            if (v.applyStroke) {
                out.strokeColor = plain(v.strokeColor);
                out.strokeWidth = v.strokeWidth;
            }
            if (v.tracking) {
                out.tracking = v.tracking;
            }
            if (v.autoLeading === false) {
                out.leading = v.leading;
            }
            if (v.allCaps) {
                out.allCaps = true;
            }
            return out;
        }
        return String(v);
    }

    // --- lookups ---

    function itemType(it) {
        if (it instanceof CompItem) {
            return "comp";
        }
        if (it instanceof FolderItem) {
            return "folder";
        }
        if (it instanceof FootageItem) {
            return "footage";
        }
        return "other";
    }

    function allItems() {
        var out = [], i;
        for (i = 1; i <= app.project.numItems; i++) {
            out.push(app.project.item(i));
        }
        return out;
    }

    function findComp(ref) {
        var items = allItems(), hits = [], i, it, ids = [];
        if (ref === undefined || ref === null) {
            if (app.project.activeItem instanceof CompItem) {
                return app.project.activeItem;
            }
            fail("no comp given and no active comp (open one, or pass comp)");
        }
        for (i = 0; i < items.length; i++) {
            it = items[i];
            if (it instanceof CompItem && (it.id === ref || it.name === ref)) {
                hits.push(it);
            }
        }
        if (hits.length === 0) {
            fail("comp not found: " + ref + " (see list_items)");
        }
        if (hits.length > 1) {
            for (i = 0; i < hits.length; i++) {
                ids.push(hits[i].id);
            }
            fail("several comps are named " + ref + ": pass the id, one of " + ids.join(", "));
        }
        return hits[0];
    }

    function findItem(ref) {
        var items = allItems(), i;
        for (i = 0; i < items.length; i++) {
            if (items[i].id === ref || items[i].name === ref) {
                return items[i];
            }
        }
        fail("project item not found: " + ref + " (see list_items)");
    }

    function findLayer(comp, ref) {
        var i;
        if (typeof ref === "number") {
            if (ref < 1 || ref > comp.numLayers) {
                fail("layer " + ref + " not found in " + comp.name + " (" + comp.numLayers + " layers)");
            }
            return comp.layer(ref);
        }
        for (i = 1; i <= comp.numLayers; i++) {
            if (comp.layer(i).name === ref) {
                return comp.layer(i);
            }
        }
        fail("layer not found in " + comp.name + ": " + ref + " (see comp_info)");
    }

    // Short names for the transform properties; anything else is a path of names or match names.
    var SHORT = {
        anchor: ["ADBE Transform Group", "ADBE Anchor Point"],
        position: ["ADBE Transform Group", "ADBE Position"],
        scale: ["ADBE Transform Group", "ADBE Scale"],
        rotation: ["ADBE Transform Group", "ADBE Rotate Z"],
        opacity: ["ADBE Transform Group", "ADBE Opacity"],
        text: ["ADBE Text Properties", "ADBE Text Document"]
    };

    function childNames(group) {
        var names = [], i;
        for (i = 1; i <= group.numProperties; i++) {
            names.push(group.property(i).name);
        }
        return names.join(", ");
    }

    function findProperty(layer, path) {
        var parts = typeof path === "string" ? (SHORT[path] || [path]) : path, p = layer, i, next;
        for (i = 0; i < parts.length; i++) {
            next = p.property(parts[i]);
            if (!next) {
                fail("no property " + parts[i] + " under " + (p.name || "the layer") + " (has: " + childNames(p) + ")");
            }
            p = next;
        }
        if (p.propertyType !== PropertyType.PROPERTY) {
            fail(p.name + " is a group, not a property (has: " + childNames(p) + ")");
        }
        return p;
    }

    function textValue(prop, v) {
        var doc = prop.value;
        if (typeof v === "string") {
            doc.text = v;
            return doc;
        }
        if (v.text !== undefined) {
            doc.text = v.text;
        }
        if (v.font !== undefined) {
            doc.font = v.font;
        }
        if (v.fontSize !== undefined) {
            doc.fontSize = v.fontSize;
        }
        if (v.fillColor !== undefined) {
            doc.applyFill = true;
            doc.fillColor = v.fillColor;
        }
        if (v.tracking !== undefined) {
            doc.tracking = v.tracking;
        }
        if (v.leading !== undefined) {
            doc.autoLeading = false;
            doc.leading = v.leading;
        }
        if (v.strokeColor !== undefined) {
            doc.applyStroke = true;
            doc.strokeColor = v.strokeColor;
        }
        if (v.strokeWidth !== undefined) {
            doc.strokeWidth = v.strokeWidth;
        }
        if (v.allCaps !== undefined) {
            doc.allCaps = v.allCaps;
        }
        if (v.justification !== undefined) {
            doc.justification = {left: ParagraphJustification.LEFT_JUSTIFY, center: ParagraphJustification.CENTER_JUSTIFY,
                right: ParagraphJustification.RIGHT_JUSTIFY}[v.justification];
        }
        return doc;
    }

    function toValue(prop, v) {
        return prop.propertyValueType === PropertyValueType.TEXT_DOCUMENT ? textValue(prop, v) : v;
    }

    function layerInfo(l) {
        var effects = [], parade = l.property("ADBE Effect Parade"), i;
        if (parade) {
            for (i = 1; i <= parade.numProperties; i++) {
                effects.push(parade.property(i).name);
            }
        }
        return {
            index: l.index, name: l.name, type: layerType(l), enabled: l.enabled,
            inPoint: round(l.inPoint), outPoint: round(l.outPoint), startTime: round(l.startTime),
            parent: l.parent ? l.parent.index : null, effects: effects,
            source: l.source ? l.source.name : null
        };
    }

    function layerType(l) {
        if (l instanceof TextLayer) {
            return "text";
        }
        if (l instanceof ShapeLayer) {
            return "shape";
        }
        if (l instanceof CameraLayer) {
            return "camera";
        }
        if (l instanceof LightLayer) {
            return "light";
        }
        if (l.nullLayer) {
            return "null";
        }
        if (l.adjustmentLayer) {
            return "adjustment";
        }
        if (l.source && l.source.mainSource && l.source.mainSource instanceof SolidSource) {
            return "solid";
        }
        return l.source instanceof CompItem ? "precomp" : "footage";
    }

    function compInfo(c) {
        var layers = [], i;
        for (i = 1; i <= c.numLayers; i++) {
            layers.push(layerInfo(c.layer(i)));
        }
        return {id: c.id, name: c.name, width: c.width, height: c.height, duration: round(c.duration),
            frameRate: round(c.frameRate), pixelAspect: c.pixelAspect, layers: layers};
    }

    // --- commands ---

    var EASE = {linear: 1, ease: 1, ease_in: 1, ease_out: 1, hold: 1};

    function keyAt(p, time, value, kind, influence) {
        // set a keyframe and its interpolation; returns its index
        var idx;
        p.setValueAtTime(time, value);
        idx = p.nearestKeyIndex(time);
        easeKey(p, idx, kind, influence);
        return idx;
    }

    function easeKey(p, idx, kind, influence) {
        var dims, eases = [], low = [], j;
        if (kind === "hold") {
            p.setInterpolationTypeAtKey(idx, KeyframeInterpolationType.HOLD, KeyframeInterpolationType.HOLD);
        } else if (kind === "linear") {
            p.setInterpolationTypeAtKey(idx, KeyframeInterpolationType.LINEAR, KeyframeInterpolationType.LINEAR);
        } else if (p.propertyValueType !== PropertyValueType.TEXT_DOCUMENT) {
            // one ease per dimension, except spatial properties (position) which take a single one
            dims = (p.value instanceof Array && !p.isSpatial) ? p.value.length : 1;
            for (j = 0; j < dims; j++) {
                eases.push(new KeyframeEase(0, influence || 33.33));
                low.push(new KeyframeEase(0, 0.1));  // 0.1 is the smallest influence: no easing
            }
            p.setInterpolationTypeAtKey(idx, KeyframeInterpolationType.BEZIER, KeyframeInterpolationType.BEZIER);
            // ease_in slows into the key (incoming side), ease_out slows out of it (outgoing side)
            p.setTemporalEaseAtKey(idx, kind === "ease_out" ? low : eases, kind === "ease_in" ? low : eases);
        }
    }

    function contains(list, v) {
        var i;
        for (i = 0; i < list.length; i++) {
            if (list[i] === v) {
                return true;
            }
        }
        return false;
    }

    function findItemOfType(ref, type, what) {
        var it = findItem(ref);
        if (!(it instanceof type)) {
            fail(it.name + " is not " + what);
        }
        return it;
    }

    function transform(l, match) {
        return l.property("ADBE Transform Group").property(match);
    }

    function enumValue(table, name, what) {
        var key = String(name).toUpperCase(), v = table[key];
        if (v === undefined) {
            fail("unknown " + what + ": " + name);
        }
        return v;
    }

    function ellipse(x, y, w, h) {
        // four Bezier points, clockwise from the top; k makes quarter arcs round
        var s = new Shape(), k = 0.5523, rx = w / 2, ry = h / 2, cx = x + rx, cy = y + ry;
        s.vertices = [[cx, y], [x + w, cy], [cx, y + h], [x, cy]];
        s.inTangents = [[-rx * k, 0], [0, -ry * k], [rx * k, 0], [0, ry * k]];
        s.outTangents = [[rx * k, 0], [0, ry * k], [-rx * k, 0], [0, -ry * k]];
        s.closed = true;
        return s;
    }

    function polygon(points) {
        var s = new Shape();
        s.vertices = points;
        s.closed = true;
        return s;
    }

    function tree(group, depth) {
        var out = [], i, p, row;
        for (i = 1; i <= group.numProperties; i++) {
            p = group.property(i);
            row = {name: p.name, matchName: p.matchName};
            if (p.propertyType === PropertyType.PROPERTY) {
                row.value = p.propertyValueType === PropertyValueType.NO_VALUE ? null : plain(p.value);
                if (p.numKeys) {
                    row.keys = p.numKeys;
                }
            } else if (depth > 1) {
                row.children = tree(p, depth - 1);
            } else {
                row.children = p.numProperties;  // count only, below the requested depth
            }
            out.push(row);
        }
        return out;
    }

    function contentRect(l) {
        // what the layer draws, in layer space: text and shapes from sourceRectAtTime, others from the source
        var r;
        if (typeof l.sourceRectAtTime === "function" && !(l.source && l.source.width)) {
            r = l.sourceRectAtTime(l.inPoint, false);
            return {left: r.left, top: r.top, width: r.width, height: r.height};
        }
        if (l.source && l.source.width) {
            return {left: 0, top: 0, width: l.source.width, height: l.source.height};
        }
        fail(l.name + " has no size to measure (cameras, lights and nulls)");
    }

    // text reveal presets: [animator property match name, value that hides a character]
    var TEXT_PRESETS = {
        fade_in: [["ADBE Text Opacity", 0]],
        typewriter: [["ADBE Text Opacity", 0]],
        slide_up: [["ADBE Text Opacity", 0], ["ADBE Text Position 3D", [0, 60, 0]]],
        scale_in: [["ADBE Text Opacity", 0], ["ADBE Text Scale 3D", [0, 0, 100]]],
        blur_in: [["ADBE Text Opacity", 0], ["ADBE Text Blur", [30, 30]]]
    };

    function statusName(s) {
        var names = ["DONE", "ERR_STOPPED", "USER_STOPPED", "RENDERING", "QUEUED", "UNQUEUED", "NEEDS_OUTPUT",
            "WILL_CONTINUE"], i;
        for (i = 0; i < names.length; i++) {
            if (RQItemStatus[names[i]] === s) {
                return names[i].toLowerCase();
            }
        }
        return String(s);
    }

    var commands = {
        status: function () {
            var active = app.project.activeItem;
            return {version: app.version, project: app.project.file ? app.project.file.fsName : null,
                items: app.project.numItems, activeComp: active instanceof CompItem ? active.name : null};
        },

        list_items: function () {
            var items = allItems(), out = [], i, it, row;
            for (i = 0; i < items.length; i++) {
                it = items[i];
                row = {id: it.id, name: it.name, type: itemType(it),
                    folder: it.parentFolder && it.parentFolder !== app.project.rootFolder ? it.parentFolder.name : null};
                if (it instanceof CompItem || it instanceof FootageItem) {
                    row.width = it.width;
                    row.height = it.height;
                    row.duration = round(it.duration);
                    row.frameRate = round(it.frameRate);
                }
                if (it instanceof FootageItem && it.file) {
                    row.file = it.file.fsName;
                }
                out.push(row);
            }
            return out;
        },

        create_comp: function (a) {
            var c = app.project.items.addComp(a.name, a.width, a.height, a.pixelAspect, a.duration, a.frameRate);
            if (a.bgColor) {
                c.bgColor = a.bgColor;
            }
            c.openInViewer();
            return compInfo(c);
        },

        comp_info: function (a) {
            return compInfo(findComp(a.comp));
        },

        add_layer: function (a) {
            var c = findComp(a.comp), l, kind = a.kind;
            if (kind === "text") {
                l = c.layers.addText(a.text || "Text");
            } else if (kind === "solid" || kind === "adjustment") {
                l = c.layers.addSolid(a.color || [0, 0, 0], a.name || (kind === "solid" ? "Solid" : "Adjustment"),
                    c.width, c.height, c.pixelAspect, c.duration);
                if (kind === "adjustment") {
                    l.adjustmentLayer = true;
                }
            } else if (kind === "shape") {
                l = c.layers.addShape();
            } else if (kind === "null") {
                l = c.layers.addNull(c.duration);
            } else if (kind === "camera") {
                l = c.layers.addCamera(a.name || "Camera", [c.width / 2, c.height / 2]);
            } else if (kind === "light") {
                l = c.layers.addLight(a.name || "Light", [c.width / 2, c.height / 2]);
            } else if (kind === "item") {
                l = c.layers.add(findItem(a.item));
            } else {
                fail("unknown layer kind: " + kind);
            }
            if (a.name && kind !== "camera" && kind !== "light") {
                l.name = a.name;
            }
            return layerInfo(l);
        },

        delete_layer: function (a) {
            var c = findComp(a.comp), l = findLayer(c, a.layer), name = l.name;
            l.remove();
            return {deleted: name, layers: c.numLayers};
        },

        set_layer: function (a) {
            var c = findComp(a.comp), l = findLayer(c, a.layer);
            if (a.name !== undefined) {
                l.name = a.name;
            }
            if (a.startTime !== undefined) {
                l.startTime = a.startTime;
            }
            if (a.inPoint !== undefined) {
                l.inPoint = a.inPoint;
            }
            if (a.outPoint !== undefined) {
                l.outPoint = a.outPoint;
            }
            if (a.enabled !== undefined) {
                l.enabled = a.enabled;
            }
            if (a.parent !== undefined) {
                l.parent = a.parent === null ? null : findLayer(c, a.parent);
            }
            return layerInfo(l);
        },

        get_property: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), p = findProperty(l, a.property), keys = [], i;
            for (i = 1; i <= p.numKeys; i++) {
                keys.push({time: round(p.keyTime(i)), value: plain(p.keyValue(i))});
            }
            return {property: p.name, matchName: p.matchName, value: plain(p.value), keys: keys,
                expression: p.expressionEnabled ? p.expression : null};
        },

        set_property: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), p = findProperty(l, a.property);
            if (p.numKeys > 0) {
                fail(p.name + " is animated (" + p.numKeys + " keyframes): use set_keyframes, or clear them first");
            }
            p.setValue(toValue(p, a.value));
            return {property: p.name, value: plain(p.value)};
        },

        set_keyframes: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), p = findProperty(l, a.property), i, k, kind, times = [];
            if (a.clear) {
                while (p.numKeys > 0) {
                    p.removeKey(p.numKeys);
                }
            }
            for (i = 0; i < a.keys.length; i++) {
                k = a.keys[i];
                kind = k.ease || "ease";
                if (!EASE[kind]) {
                    fail("ease must be linear, ease, ease_in, ease_out or hold (got " + kind + ")");
                }
                times.push(round(p.keyTime(keyAt(p, k.time, toValue(p, k.value), kind, k.influence))));
            }
            return {property: p.name, keys: p.numKeys, times: times};
        },

        set_expression: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), p = findProperty(l, a.property), err;
            if (!p.canSetExpression) {
                fail(p.name + " cannot take an expression");
            }
            p.expression = a.expression || "";
            p.expressionEnabled = !!a.expression;
            err = p.expressionError || "";
            return {property: p.name, expression: p.expression, enabled: p.expressionEnabled,
                error: err ? err : null, value: plain(p.value)};
        },

        add_effect: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), parade = l.property("ADBE Effect Parade"), fx, k, prop,
                set = {};
            if (!parade.canAddProperty(a.effect)) {
                fail("unknown effect: " + a.effect + " (use the display or match name from list_effects)");
            }
            fx = parade.addProperty(a.effect);
            if (a.name) {
                fx.name = a.name;
            }
            for (k in a.settings) {
                if (a.settings.hasOwnProperty(k)) {
                    prop = fx.property(k);
                    if (!prop) {
                        fail(fx.name + " was added but has no parameter " + k + " (has: " + childNames(fx) + ")");
                    }
                    prop.setValue(a.settings[k]);
                    set[k] = plain(prop.value);
                }
            }
            return {layer: l.name, effect: fx.name, matchName: fx.matchName, set: set};
        },

        list_effects: function (a) {
            var out = [], i, e, q = a.query ? a.query.toLowerCase() : null;
            for (i = 0; i < app.effects.length; i++) {
                e = app.effects[i];
                if (q && (e.displayName + " " + e.matchName + " " + e.category).toLowerCase().indexOf(q) < 0) {
                    continue;
                }
                out.push({name: e.displayName, matchName: e.matchName, category: e.category});
            }
            return out;
        },

        import_file: function (a) {
            var f = new File(a.path), opts, it;
            if (!f.exists) {
                fail("file not found: " + a.path);
            }
            opts = new ImportOptions(f);
            if (a.sequence) {
                opts.sequence = true;
            }
            it = app.project.importFile(opts);
            if (a.folder) {
                it.parentFolder = findItem(a.folder);
            }
            return {id: it.id, name: it.name, type: itemType(it)};
        },

        add_to_render_queue: function (a) {
            var c = findComp(a.comp), rq = app.project.renderQueue.items.add(c), om = rq.outputModule(1);
            try {
                if (a.template) {
                    om.applyTemplate(a.template);
                }
                om.file = new File(a.output);
            } catch (e) {
                rq.remove();  // a half-set queue item would stop the next render
                throw e;
            }
            return {comp: c.name, index: app.project.renderQueue.numItems, output: om.file.fsName};
        },

        render: function () {
            var rq = app.project.renderQueue, queued = 0, i, out = [];
            for (i = 1; i <= rq.numItems; i++) {
                if (rq.item(i).status === RQItemStatus.QUEUED) {
                    queued += 1;
                }
            }
            if (!queued) {
                fail("nothing queued (see add_to_render_queue)");
            }
            rq.render();  // blocks After Effects until the queue is done
            for (i = 1; i <= rq.numItems; i++) {
                out.push({comp: rq.item(i).comp.name, status: statusName(rq.item(i).status),
                    output: rq.item(i).outputModule(1).file ? rq.item(i).outputModule(1).file.fsName : null});
            }
            return {rendered: queued, items: out};
        },

        export_frame: function (a) {
            var c = findComp(a.comp), f = new File(a.path);
            c.saveFrameToPng(a.time, f);
            return {comp: c.name, time: a.time, path: f.fsName};
        },

        save_project: function (a) {
            if (a.path) {
                app.project.save(new File(a.path));
            } else if (app.project.file) {
                app.project.save();
            } else {
                fail("the project was never saved: pass a path");
            }
            return {project: app.project.file.fsName};
        },

        add_mask: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), group = l.property("ADBE Mask Parade"), m, shape, r;
            if (!group) {
                fail(l.name + " cannot take masks (cameras and lights have none)");
            }
            if (a.shape === "rect") {
                r = a.rect;
                shape = polygon([[r[0], r[1]], [r[0] + r[2], r[1]], [r[0] + r[2], r[1] + r[3]], [r[0], r[1] + r[3]]]);
            } else if (a.shape === "ellipse") {
                shape = ellipse(a.rect[0], a.rect[1], a.rect[2], a.rect[3]);
            } else {
                shape = polygon(a.points);
            }
            m = group.addProperty("ADBE Mask Atom");
            m.property("ADBE Mask Shape").setValue(shape);
            m.maskMode = enumValue(MaskMode, a.mode || "add", "mask mode");
            if (a.feather !== undefined) {
                m.property("ADBE Mask Feather").setValue([a.feather, a.feather]);
            }
            if (a.expansion !== undefined) {
                m.property("ADBE Mask Offset").setValue(a.expansion);
            }
            if (a.opacity !== undefined) {
                m.property("ADBE Mask Opacity").setValue(a.opacity);
            }
            if (a.inverted) {
                m.inverted = true;
            }
            if (a.name) {
                m.name = a.name;
            }
            return {layer: l.name, mask: m.name, masks: group.numProperties, mode: a.mode || "add",
                vertices: shape.vertices.length};
        },

        add_shape: function (a) {
            var c = findComp(a.comp), l = a.layer !== undefined ? findLayer(c, a.layer) : c.layers.addShape(),
                root = l.property("ADBE Root Vectors Group"), grp, contents, path, fill, stroke;
            if (!root) {
                fail(l.name + " is not a shape layer");
            }
            if (a.layerName && a.layer === undefined) {
                l.name = a.layerName;
            }
            grp = root.addProperty("ADBE Vector Group");
            if (a.name) {
                grp.name = a.name;
            }
            contents = grp.property("ADBE Vectors Group");
            if (a.shape === "rect") {
                path = contents.addProperty("ADBE Vector Shape - Rect");
                path.property("ADBE Vector Rect Size").setValue(a.size);
                if (a.roundness) {
                    path.property("ADBE Vector Rect Roundness").setValue(a.roundness);
                }
            } else if (a.shape === "ellipse") {
                path = contents.addProperty("ADBE Vector Shape - Ellipse");
                path.property("ADBE Vector Ellipse Size").setValue(a.size);
            } else {
                path = contents.addProperty("ADBE Vector Shape - Star");
                path.property("ADBE Vector Star Type").setValue(a.shape === "star" ? 1 : 2);
                path.property("ADBE Vector Star Points").setValue(a.points);
                path.property("ADBE Vector Star Outer Radius").setValue(a.size[0] / 2);
                if (a.shape === "star") {
                    path.property("ADBE Vector Star Inner Radius").setValue(a.size[0] / 4);
                }
            }
            if (a.fill) {
                fill = contents.addProperty("ADBE Vector Graphic - Fill");
                fill.property("ADBE Vector Fill Color").setValue(a.fill);
            }
            if (a.stroke) {
                stroke = contents.addProperty("ADBE Vector Graphic - Stroke");
                stroke.property("ADBE Vector Stroke Color").setValue(a.stroke);
                stroke.property("ADBE Vector Stroke Width").setValue(a.strokeWidth);
            }
            if (a.position) {
                grp.property("ADBE Vector Transform Group").property("ADBE Vector Position").setValue(a.position);
            }
            return {layer: l.name, index: l.index, group: grp.name, shape: a.shape};
        },

        animate_text: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), text = l.property("ADBE Text Properties"), anim, props,
                sel, start, preset = TEXT_PRESETS[a.preset], i, p, t0, t1, idx, kind;
            if (!text) {
                fail(l.name + " is not a text layer");
            }
            if (!preset) {
                fail("unknown preset: " + a.preset);
            }
            anim = text.property("ADBE Text Animators").addProperty("ADBE Text Animator");
            anim.name = "aemcp " + a.preset;
            props = anim.property("ADBE Text Animator Properties");
            for (i = 0; i < preset.length; i++) {
                p = props.addProperty(preset[i][0]);
                p.setValue(preset[i][1]);
            }
            sel = anim.property("ADBE Text Selectors").addProperty("ADBE Text Selector");
            if (a.preset === "typewriter") {
                // hard steps: each character appears whole
                sel.property("ADBE Text Range Advanced").property("ADBE Text Selector Smoothness").setValue(0);
            }
            // the selector covers the characters still hidden: moving its start from 0 to 100 reveals them in order
            start = sel.property("ADBE Text Percent Start");
            t0 = a.start !== undefined ? a.start : l.inPoint;
            t1 = t0 + a.duration;
            kind = a.preset === "typewriter" ? KeyframeInterpolationType.LINEAR : KeyframeInterpolationType.BEZIER;
            start.setValueAtTime(t0, 0);
            start.setValueAtTime(t1, 100);
            for (i = 1; i <= start.numKeys; i++) {
                start.setInterpolationTypeAtKey(i, kind, kind);
            }
            idx = start.numKeys;
            return {layer: l.name, animator: anim.name, preset: a.preset, from: round(t0), to: round(t1), keys: idx};
        },

        precompose: function (a) {
            var c = findComp(a.comp), idx = [], i, nc, j, layer = null;
            for (i = 0; i < a.layers.length; i++) {
                idx.push(findLayer(c, a.layers[i]).index);
            }
            nc = c.layers.precompose(idx, a.name, a.moveAttributes);
            for (j = 1; j <= c.numLayers; j++) {
                if (c.layer(j).source === nc) {
                    layer = j;
                }
            }
            return {comp: c.name, precomp: nc.name, id: nc.id, precompLayers: nc.numLayers, layer: layer};
        },

        duplicate_layer: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), d = l.duplicate();
            if (a.name) {
                d.name = a.name;
            }
            return layerInfo(d);
        },

        move_layer: function (a) {
            var c = findComp(a.comp), l = findLayer(c, a.layer), other;
            if (a.to === "top") {
                l.moveToBeginning();
            } else if (a.to === "bottom") {
                l.moveToEnd();
            } else {
                other = findLayer(c, a.other);
                if (other === l) {
                    fail("a layer cannot move relative to itself");
                }
                if (a.to === "before") {
                    l.moveBefore(other);
                } else {
                    l.moveAfter(other);
                }
            }
            return {layer: l.name, index: l.index};
        },

        set_switches: function (a) {
            var c = findComp(a.comp), l = findLayer(c, a.layer), out = {};
            if (a.blending !== undefined) {
                l.blendingMode = enumValue(BlendingMode, a.blending, "blending mode");
            }
            if (a.threeD !== undefined) {
                l.threeDLayer = a.threeD;
            }
            if (a.motionBlur !== undefined) {
                l.motionBlur = a.motionBlur;
            }
            if (a.shy !== undefined) {
                l.shy = a.shy;
            }
            if (a.solo !== undefined) {
                l.solo = a.solo;
            }
            if (a.matte !== undefined) {
                if (a.matte === "none") {
                    l.removeTrackMatte();
                } else {
                    if (typeof l.setTrackMatte !== "function") {
                        fail("setting a track matte needs After Effects 2023 or later");
                    }
                    l.setTrackMatte(findLayer(c, a.matteLayer), enumValue(TrackMatteType, a.matte, "matte type"));
                }
            }
            if (a.locked !== undefined) {
                l.locked = a.locked;  // last: a locked layer takes no more changes
            }
            out = {layer: l.name, threeD: l.threeDLayer, motionBlur: l.motionBlur, shy: l.shy, solo: l.solo,
                locked: l.locked, matteLayer: l.trackMatteLayer ? l.trackMatteLayer.name : null};
            return out;
        },

        add_marker: function (a) {
            var c = findComp(a.comp), prop, mv, target;
            if (a.layer !== undefined) {
                target = findLayer(c, a.layer);
                prop = target.property("ADBE Marker");
            } else {
                prop = c.markerProperty;
            }
            if (!prop) {
                fail("no marker track here (comp markers need After Effects 2017 or later)");
            }
            mv = new MarkerValue(a.comment || "");
            if (a.duration) {
                mv.duration = a.duration;
            }
            prop.setValueAtTime(a.time, mv);
            return {target: target ? target.name : c.name, time: a.time, markers: prop.numKeys};
        },

        set_comp: function (a) {
            var c = findComp(a.comp);
            if (a.name !== undefined) {
                c.name = a.name;
            }
            if (a.width !== undefined) {
                c.width = a.width;
            }
            if (a.height !== undefined) {
                c.height = a.height;
            }
            if (a.frameRate !== undefined) {
                c.frameRate = a.frameRate;
            }
            if (a.duration !== undefined) {
                c.duration = a.duration;
            }
            if (a.bgColor !== undefined) {
                c.bgColor = a.bgColor;
            }
            if (a.workArea !== undefined) {
                if (a.workArea[0] < 0 || a.workArea[1] <= a.workArea[0] || a.workArea[1] > c.duration) {
                    fail("work_area must be [start, end] inside the comp (0-" + round(c.duration) + " s)");
                }
                c.workAreaStart = 0;  // shrink first so the new start always fits
                c.workAreaDuration = a.workArea[1] - a.workArea[0];
                c.workAreaStart = a.workArea[0];
            }
            return {id: c.id, name: c.name, width: c.width, height: c.height, duration: round(c.duration),
                frameRate: round(c.frameRate), workArea: [round(c.workAreaStart),
                    round(c.workAreaStart + c.workAreaDuration)]};
        },

        render_templates: function () {
            var comps = allItems(), comp = null, i, rq, out;
            for (i = 0; i < comps.length; i++) {
                if (comps[i] instanceof CompItem) {
                    comp = comps[i];
                    break;
                }
            }
            if (!comp) {
                fail("the project has no comp: create one first (the template lists come from a render queue item)");
            }
            rq = app.project.renderQueue.items.add(comp);
            try {
                out = {outputModules: plain(rq.outputModule(1).templates), renderSettings: plain(rq.templates)};
            } finally {
                rq.remove();
            }
            return out;
        },

        property_tree: function (a) {
            var l = findLayer(findComp(a.comp), a.layer);
            return {layer: l.name, properties: tree(l, a.depth)};
        },

        sequence_layers: function (a) {
            var c = findComp(a.comp), t = a.start, i, l, out = [];
            for (i = 0; i < a.layers.length; i++) {
                l = findLayer(c, a.layers[i]);
                l.startTime += t - l.inPoint;  // moving startTime moves in and out with it
                out.push({layer: l.name, inPoint: round(l.inPoint), outPoint: round(l.outPoint)});
                t = l.outPoint - a.overlap;
            }
            return {layers: out, end: round(t + a.overlap)};
        },

        time_remap: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), p, i, k, idx, kind, oldTime, keepOld = false;
            if (!l.canSetTimeRemapEnabled) {
                fail(l.name + " cannot be time remapped (it needs footage or a precomp with duration)");
            }
            l.timeRemapEnabled = true;
            p = l.property("ADBE Time Remapping");
            // After Effects adds keys at the in and out points; the given ones replace them. Removing the last key
            // turns time remapping off, so one old key stays until the new ones are in.
            while (p.numKeys > 1) {
                p.removeKey(p.numKeys);
            }
            oldTime = p.numKeys ? p.keyTime(1) : null;
            for (i = 0; i < a.keys.length; i++) {
                k = a.keys[i];
                if (oldTime !== null && Math.abs(k.time - oldTime) < 1e-6) {
                    keepOld = true;
                }
                p.setValueAtTime(k.time, k.source);
                idx = p.nearestKeyIndex(k.time);
                kind = k.hold ? KeyframeInterpolationType.HOLD : (a.smooth ? KeyframeInterpolationType.BEZIER :
                    KeyframeInterpolationType.LINEAR);
                p.setInterpolationTypeAtKey(idx, kind, kind);
            }
            if (oldTime !== null && !keepOld && p.numKeys > 1) {
                p.removeKey(p.nearestKeyIndex(oldTime));
            }
            return {layer: l.name, keys: p.numKeys, timeRemap: l.timeRemapEnabled};
        },

        fit_to_comp: function (a) {
            var c = findComp(a.comp), l = findLayer(c, a.layer), r = contentRect(l), sx = c.width / r.width,
                sy = c.height / r.height, s, scale;
            if (a.mode === "fill") {
                s = Math.max(sx, sy);
                scale = [s * 100, s * 100];
            } else if (a.mode === "fit") {
                s = Math.min(sx, sy);
                scale = [s * 100, s * 100];
            } else if (a.mode === "width") {
                scale = [sx * 100, sx * 100];
            } else if (a.mode === "height") {
                scale = [sy * 100, sy * 100];
            } else {
                scale = [sx * 100, sy * 100];
            }
            l.property("ADBE Transform Group").property("ADBE Anchor Point")
                .setValue([r.left + r.width / 2, r.top + r.height / 2]);
            l.property("ADBE Transform Group").property("ADBE Position").setValue([c.width / 2, c.height / 2]);
            l.property("ADBE Transform Group").property("ADBE Scale").setValue(scale);
            return {layer: l.name, mode: a.mode, scale: [round(scale[0], 2), round(scale[1], 2)],
                size: [round(r.width), round(r.height)]};
        },

        center_anchor: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), tg = l.property("ADBE Transform Group"),
                anchor = tg.property("ADBE Anchor Point"), pos = tg.property("ADBE Position"),
                scale = tg.property("ADBE Scale").value, r = contentRect(l), oldA = anchor.value, p = pos.value,
                newA = [r.left + r.width / 2, r.top + r.height / 2];
            if (anchor.numKeys || pos.numKeys) {
                fail("anchor point or position is animated: center it before animating");
            }
            anchor.setValue(newA);
            // keep the layer where it is: move the position by the anchor shift, scaled (rotation ignored)
            pos.setValue([p[0] + (newA[0] - oldA[0]) * scale[0] / 100, p[1] + (newA[1] - oldA[1]) * scale[1] / 100]);
            return {layer: l.name, anchor: [round(newA[0], 2), round(newA[1], 2)], position: plain(pos.value)};
        },

        null_control: function (a) {
            var c = findComp(a.comp), layers = [], i, n, x = 0, y = 0, pos, names = [];
            for (i = 0; i < a.layers.length; i++) {
                layers.push(findLayer(c, a.layers[i]));
            }
            for (i = 0; i < layers.length; i++) {
                pos = layers[i].property("ADBE Transform Group").property("ADBE Position").value;
                x += pos[0];
                y += pos[1];
            }
            n = c.layers.addNull(c.duration);
            n.name = a.name;
            n.property("ADBE Transform Group").property("ADBE Position").setValue([x / layers.length, y / layers.length]);
            for (i = 0; i < layers.length; i++) {
                layers[i].parent = n;  // parenting keeps each layer where it is on screen
                names.push(layers[i].name);
            }
            return {control: n.name, index: n.index, children: names};
        },

        apply_preset: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), f = new File(a.path);
            if (!f.exists) {
                fail("preset not found: " + a.path);
            }
            l.applyPreset(f);
            return layerInfo(l);
        },

        replace_footage: function (a) {
            var it = findItem(a.item), f = new File(a.path);
            if (!(it instanceof FootageItem)) {
                fail(it.name + " is not footage");
            }
            if (!f.exists) {
                fail("file not found: " + a.path);
            }
            if (a.sequence) {
                it.replaceWithSequence(f, false);
            } else {
                it.replace(f);
            }
            return {id: it.id, name: it.name, file: it.file ? it.file.fsName : null};
        },

        create_folder: function (a) {
            var f = app.project.items.addFolder(a.name), parent;
            if (a.parent !== undefined) {
                parent = findItem(a.parent);
                if (!(parent instanceof FolderItem)) {
                    fail(parent.name + " is not a folder");
                }
                f.parentFolder = parent;
            }
            return {id: f.id, name: f.name, parent: a.parent !== undefined ? parent.name : null};
        },

        move_items: function (a) {
            var folder = findItem(a.folder), i, it, moved = [];
            if (!(folder instanceof FolderItem)) {
                fail(folder.name + " is not a folder");
            }
            for (i = 0; i < a.items.length; i++) {
                it = findItem(a.items[i]);
                if (it === folder) {
                    fail("a folder cannot go into itself");
                }
                it.parentFolder = folder;
                moved.push(it.name);
            }
            return {folder: folder.name, moved: moved};
        },

        clean_project: function (a) {
            var before = app.project.numItems, out = {};
            if (a.consolidate) {
                out.consolidated = app.project.consolidateFootage();
            }
            if (a.removeUnused) {
                out.removedUnused = app.project.removeUnusedFootage();
            }
            out.items = [before, app.project.numItems];
            return out;
        },

        camera_move: function (a) {
            var c = findComp(a.comp), cam, pos, poi, p0, q0, p1, q1, d, len, t0 = a.start, t1 = a.start + a.duration,
                kind = a.ease ? "ease" : "linear", rig = null, rot, out;
            if (a.camera !== undefined) {
                cam = findLayer(c, a.camera);
                if (!(cam instanceof CameraLayer)) {
                    fail(cam.name + " is not a camera");
                }
            } else {
                cam = c.layers.addCamera("Camera", [c.width / 2, c.height / 2]);
            }
            pos = transform(cam, "ADBE Position");
            poi = transform(cam, "ADBE Anchor Point");  // a camera's anchor point is its point of interest
            p0 = pos.valueAtTime(t0, false);
            q0 = poi.valueAtTime(t0, false);
            if (a.move === "orbit") {
                // a 3D null at the point of interest carries the camera around it
                rig = c.layers.addNull(c.duration);
                rig.name = cam.name + " Orbit";
                rig.threeDLayer = true;
                transform(rig, "ADBE Position").setValue([q0[0], q0[1], q0[2]]);
                cam.parent = rig;
                rot = transform(rig, "ADBE Rotate Y");
                keyAt(rot, t0, 0, kind);
                keyAt(rot, t1, a.amount, kind);
                return {camera: cam.name, move: a.move, rig: rig.name, from: round(t0), to: round(t1), degrees: a.amount};
            }
            if (a.move === "push_in" || a.move === "pull_out") {
                d = [q0[0] - p0[0], q0[1] - p0[1], q0[2] - p0[2]];
                len = Math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
                if (len === 0) {
                    fail("the camera sits on its point of interest: no direction to move in");
                }
                len = (a.move === "push_in" ? a.amount : -a.amount) / len;
                p1 = [p0[0] + d[0] * len, p0[1] + d[1] * len, p0[2] + d[2] * len];
                q1 = q0;
            } else {
                d = {pan_left: [-1, 0], pan_right: [1, 0], crane_up: [0, -1], crane_down: [0, 1]}[a.move];
                p1 = [p0[0] + d[0] * a.amount, p0[1] + d[1] * a.amount, p0[2]];
                q1 = [q0[0] + d[0] * a.amount, q0[1] + d[1] * a.amount, q0[2]];
            }
            keyAt(pos, t0, p0, kind);
            keyAt(pos, t1, p1, kind);
            if (q1 !== q0) {
                keyAt(poi, t0, q0, kind);
                keyAt(poi, t1, q1, kind);
            }
            out = {camera: cam.name, move: a.move, from: round(t0), to: round(t1), position: [plain(p0), plain(p1)]};
            return out;
        },

        set_light: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), opts, set = {};
            if (!(l instanceof LightLayer)) {
                fail(l.name + " is not a light");
            }
            if (a.type !== undefined) {
                l.lightType = enumValue(LightType, a.type, "light type");
            }
            opts = l.property("ADBE Light Options Group");
            function put(match, v, key) {
                var p = opts.property(match);
                if (!p) {
                    fail("this light type has no " + key);
                }
                p.setValue(v);
                set[key] = plain(p.value);
            }
            if (a.intensity !== undefined) {
                put("ADBE Light Intensity", a.intensity, "intensity");
            }
            if (a.color !== undefined) {
                put("ADBE Light Color", a.color, "color");
            }
            if (a.coneAngle !== undefined) {
                put("ADBE Light Cone Angle", a.coneAngle, "coneAngle");
            }
            if (a.coneFeather !== undefined) {
                put("ADBE Light Cone Feather 2", a.coneFeather, "coneFeather");
            }
            if (a.shadows !== undefined) {
                put("ADBE Light Shadow Casting", a.shadows ? 1 : 0, "shadows");
            }
            return {light: l.name, set: set};
        },

        audio_fade: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), p, lv = a.level, lo = -48, t0 = l.inPoint, t1 = l.outPoint;
            if (!l.hasAudio) {
                fail(l.name + " has no audio");
            }
            if (a.fadeIn + a.fadeOut > t1 - t0) {
                fail("the fades (" + (a.fadeIn + a.fadeOut) + " s) are longer than the layer (" + round(t1 - t0) + " s)");
            }
            p = l.property("ADBE Audio Group").property("ADBE Audio Levels");
            while (p.numKeys > 0) {
                p.removeKey(p.numKeys);
            }
            if (a.fadeIn > 0) {
                keyAt(p, t0, [lo, lo], "linear");
                keyAt(p, t0 + a.fadeIn, [lv, lv], "linear");
            }
            if (a.fadeOut > 0) {
                keyAt(p, t1 - a.fadeOut, [lv, lv], "linear");
                keyAt(p, t1, [lo, lo], "linear");
            }
            if (!p.numKeys) {
                p.setValue([lv, lv]);
            }
            return {layer: l.name, level: lv, fadeIn: a.fadeIn, fadeOut: a.fadeOut, keys: p.numKeys};
        },

        audio_react: function (a) {
            var c = findComp(a.comp), src = findLayer(c, a.audio), target = findLayer(c, a.layer),
                p = findProperty(target, a.property), cmd, i, amp, dims, parts = [], expr, before = c.numLayers;
            if (!src.hasAudio) {
                fail(src.name + " has no audio");
            }
            cmd = app.findMenuCommandId("Convert Audio to Keyframes");
            if (!cmd) {
                fail("Convert Audio to Keyframes was not found in the menus (a non-English After Effects?)");
            }
            c.openInViewer();
            for (i = 1; i <= c.numLayers; i++) {
                c.layer(i).selected = false;
            }
            src.selected = true;  // the command works on the selected layer of the active comp
            app.executeCommand(cmd);
            if (c.numLayers !== before + 1) {
                fail("Convert Audio to Keyframes added no layer");
            }
            amp = c.layer(1);
            amp.name = a.name;
            dims = p.value instanceof Array ? p.value.length : 1;
            expr = 'var a = thisComp.layer("' + amp.name + '").effect("Both Channels")("Slider") * ' + a.amount + ';\n';
            if (dims === 1) {
                expr += "value + a";
            } else {
                for (i = 0; i < dims; i++) {
                    parts.push(i < 2 ? "value[" + i + "] + a" : "value[" + i + "]");
                }
                expr += "[" + parts.join(", ") + "]";
            }
            p.expression = expr;
            p.expressionEnabled = true;
            return {amplitude: amp.name, target: target.name, property: p.name, expression: expr,
                error: p.expressionError || null};
        },

        add_captions: function (a) {
            var c = findComp(a.comp), i, cue, l, doc, made = [];
            for (i = 0; i < a.cues.length; i++) {
                cue = a.cues[i];
                l = c.layers.addText(cue.text);
                doc = l.property("ADBE Text Properties").property("ADBE Text Document").value;
                doc.fontSize = a.size;
                if (a.font) {
                    doc.font = a.font;
                }
                doc.applyFill = true;
                doc.fillColor = a.color;
                doc.justification = ParagraphJustification.CENTER_JUSTIFY;
                l.property("ADBE Text Properties").property("ADBE Text Document").setValue(doc);
                transform(l, "ADBE Position").setValue([c.width / 2, c.height * a.y]);
                l.inPoint = cue.start;
                l.outPoint = cue.end;
                l.name = (a.prefix || "Caption") + " " + (i + 1);
                made.push(l.name);
            }
            return {comp: c.name, captions: made.length, first: made[0] || null, last: made[made.length - 1] || null};
        },

        mogrt_add_property: function (a) {
            var c = findComp(a.comp), l = findLayer(c, a.layer), p = findProperty(l, a.property), ok;
            if (typeof p.addToMotionGraphicsTemplateAs === "function") {
                ok = p.addToMotionGraphicsTemplateAs(c, a.name || p.name);
            } else if (typeof p.addToMotionGraphicsTemplate === "function") {
                ok = p.addToMotionGraphicsTemplate(c);
            } else {
                fail("Essential Graphics scripting needs After Effects 2018 or later");
            }
            if (!ok) {
                fail(p.name + " cannot be added to the Essential Graphics panel");
            }
            return {comp: c.name, layer: l.name, property: p.name, name: a.name || p.name};
        },

        export_mogrt: function (a) {
            var c = findComp(a.comp);
            if (a.name) {
                c.motionGraphicsTemplateName = a.name;
            }
            if (!c.exportAsMotionGraphicsTemplate(true, a.path)) {
                fail("exportAsMotionGraphicsTemplate failed (add properties to Essential Graphics first)");
            }
            return {comp: c.name, template: c.motionGraphicsTemplateName, path: a.path};
        },

        bake_expression: function (a) {
            var c = findComp(a.comp), l = findLayer(c, a.layer), p = findProperty(l, a.property), times = [],
                values = [], t, dt = c.frameDuration * a.step, t0 = a.start !== undefined ? a.start : l.inPoint,
                t1 = a.end !== undefined ? a.end : l.outPoint;
            if (!p.expressionEnabled || !p.expression) {
                fail(p.name + " has no expression to bake");
            }
            if (t1 <= t0) {
                fail("end must be after start");
            }
            for (t = t0; t < t1 - 1e-9; t += dt) {
                times.push(t);
                values.push(p.valueAtTime(t, false));  // false: with the expression applied
            }
            p.expressionEnabled = false;
            while (p.numKeys > 0) {
                p.removeKey(p.numKeys);
            }
            p.setValuesAtTimes(times, values);
            return {property: p.name, keys: p.numKeys, from: round(t0), to: round(times[times.length - 1]),
                expression: "disabled (kept, not deleted)"};
        },

        ease_keyframes: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), p = findProperty(l, a.property), i;
            if (!p.numKeys) {
                fail(p.name + " has no keyframes");
            }
            for (i = 1; i <= p.numKeys; i++) {
                easeKey(p, i, a.ease, a.influence);
            }
            return {property: p.name, keys: p.numKeys, ease: a.ease};
        },

        copy_keyframes: function (a) {
            var c = findComp(a.comp), l = findLayer(c, a.layer), src = findProperty(l, a.property), keys = [], i, j,
                k, target, dst, idx, done = [];
            if (!src.numKeys) {
                fail(src.name + " on " + l.name + " has no keyframes to copy");
            }
            for (i = 1; i <= src.numKeys; i++) {
                keys.push({time: src.keyTime(i), value: src.keyValue(i), inType: src.keyInInterpolationType(i),
                    outType: src.keyOutInterpolationType(i), inEase: src.keyInTemporalEase(i),
                    outEase: src.keyOutTemporalEase(i)});
            }
            for (j = 0; j < a.targets.length; j++) {
                target = findLayer(c, a.targets[j]);
                if (target === l) {
                    fail("a layer cannot be its own target");
                }
                dst = findProperty(target, a.property);
                while (dst.numKeys > 0) {
                    dst.removeKey(dst.numKeys);
                }
                for (i = 0; i < keys.length; i++) {
                    k = keys[i];
                    dst.setValueAtTime(k.time + a.offset * (j + 1), k.value);
                    idx = dst.nearestKeyIndex(k.time + a.offset * (j + 1));
                    dst.setInterpolationTypeAtKey(idx, k.inType, k.outType);
                    if (k.outType === KeyframeInterpolationType.BEZIER || k.inType === KeyframeInterpolationType.BEZIER) {
                        dst.setTemporalEaseAtKey(idx, k.inEase, k.outEase);
                    }
                }
                done.push(target.name);
            }
            return {property: src.name, from: l.name, to: done, keys: keys.length};
        },

        stagger_layers: function (a) {
            var c = findComp(a.comp), layers = [], i, base, out = [];
            for (i = 0; i < a.layers.length; i++) {
                layers.push(findLayer(c, a.layers[i]));
            }
            base = a.start !== undefined ? a.start : layers[0].inPoint;
            for (i = 0; i < layers.length; i++) {
                layers[i].startTime += base + i * a.offset - layers[i].inPoint;
                out.push({layer: layers[i].name, inPoint: round(layers[i].inPoint)});
            }
            return {layers: out};
        },

        split_layer: function (a) {
            var c = findComp(a.comp), l = findLayer(c, a.layer), d;
            if (a.time <= l.inPoint || a.time >= l.outPoint) {
                fail("split time must be inside the layer (" + round(l.inPoint) + "-" + round(l.outPoint) + " s)");
            }
            d = l.duplicate();  // the copy sits right above the original
            l.outPoint = a.time;
            d.inPoint = a.time;
            if (a.name) {
                d.name = a.name;
            }
            return {first: {index: l.index, name: l.name, outPoint: round(l.outPoint)},
                second: {index: d.index, name: d.name, inPoint: round(d.inPoint)}};
        },

        trim_comp: function (a) {
            var c = findComp(a.comp), i, lo = null, hi = null, l, shift, dur;
            if (a.to === "work_area") {
                shift = c.workAreaStart;
                dur = c.workAreaDuration;
            } else {
                for (i = 1; i <= c.numLayers; i++) {
                    l = c.layer(i);
                    lo = lo === null ? l.inPoint : Math.min(lo, l.inPoint);
                    hi = hi === null ? l.outPoint : Math.max(hi, l.outPoint);
                }
                if (lo === null) {
                    fail(c.name + " has no layers to trim to");
                }
                shift = Math.max(0, lo);
                dur = Math.min(hi, c.duration) - shift;
            }
            for (i = 1; i <= c.numLayers; i++) {
                c.layer(i).startTime -= shift;
            }
            c.workAreaStart = 0;
            c.duration = dur;
            c.workAreaDuration = dur;
            return {comp: c.name, duration: round(dur), shiftedBy: round(shift)};
        },

        make_variants: function (a) {
            var c = findComp(a.comp), made = [], i, row, dup, k, l, prop, doc, rq;
            for (i = 0; i < a.rows.length; i++) {
                row = a.rows[i];
                dup = c.duplicate();
                dup.name = row.name;
                for (k in row.texts) {
                    if (row.texts.hasOwnProperty(k)) {
                        l = findLayer(dup, k);
                        prop = l.property("ADBE Text Properties");
                        if (!prop) {
                            fail(l.name + " in " + c.name + " is not a text layer");
                        }
                        prop = prop.property("ADBE Text Document");
                        doc = prop.value;
                        doc.text = row.texts[k];
                        prop.setValue(doc);
                    }
                }
                if (a.outputDir) {
                    rq = app.project.renderQueue.items.add(dup);
                    if (a.template) {
                        rq.outputModule(1).applyTemplate(a.template);
                    }
                    rq.outputModule(1).file = new File(a.outputDir + "/" + row.name);
                }
                made.push({comp: dup.name, id: dup.id, queued: !!a.outputDir});
            }
            return {from: c.name, variants: made};
        },

        import_layered: function (a) {
            var f = new File(a.path), opts = new ImportOptions(f), it;
            if (!f.exists) {
                fail("file not found: " + a.path);
            }
            opts.importAs = a.mode === "footage" ? ImportAsType.FOOTAGE :
                (a.mode === "comp" ? ImportAsType.COMP : ImportAsType.COMP_CROPPED_LAYERS);
            if (!opts.canImportAs(opts.importAs)) {
                fail("After Effects cannot import " + f.name + " as " + a.mode + " (layered PSD / AI files can)");
            }
            it = app.project.importFile(opts);
            return {id: it.id, name: it.name, type: itemType(it), layers: it instanceof CompItem ? it.numLayers : null};
        },

        missing_footage: function () {
            var items = allItems(), out = [], i;
            for (i = 0; i < items.length; i++) {
                if (items[i] instanceof FootageItem && items[i].footageMissing) {
                    out.push({id: items[i].id, name: items[i].name, file: items[i].file ? items[i].file.fsName : null});
                }
            }
            return out;
        },

        relink_footage: function (a) {
            var done = [], i, it;
            for (i = 0; i < a.links.length; i++) {
                it = findItemOfType(a.links[i].id, FootageItem, "footage");
                it.replace(new File(a.links[i].path));
                done.push({id: it.id, name: it.name, missing: it.footageMissing});
            }
            return done;
        },

        trim_paths: function (a) {
            var c = findComp(a.comp), l = findLayer(c, a.layer), root = l.property("ADBE Root Vectors Group"), trim,
                prop, t0 = a.start !== undefined ? a.start : l.inPoint, kind = a.ease ? "ease" : "linear";
            if (!root) {
                fail(l.name + " is not a shape layer");
            }
            trim = root.addProperty("ADBE Vector Filter - Trim");
            // draw on: the end runs 0 to 100; erase: the start runs 0 to 100
            prop = trim.property(a.erase ? "ADBE Vector Trim Start" : "ADBE Vector Trim End");
            keyAt(prop, t0, 0, kind);
            keyAt(prop, t0 + a.duration, 100, kind);
            if (a.offset) {
                trim.property("ADBE Vector Trim Offset").setValue(a.offset);
            }
            return {layer: l.name, trim: trim.name, animated: prop.name, from: round(t0), to: round(t0 + a.duration)};
        },

        shape_repeater: function (a) {
            var l = findLayer(findComp(a.comp), a.layer), root = l.property("ADBE Root Vectors Group"), rep, tr;
            if (!root) {
                fail(l.name + " is not a shape layer");
            }
            rep = root.addProperty("ADBE Vector Filter - Repeater");
            rep.property("ADBE Vector Repeater Copies").setValue(a.copies);
            tr = rep.property("ADBE Vector Repeater Transform");
            tr.property("ADBE Vector Repeater Position").setValue(a.offset);
            tr.property("ADBE Vector Repeater Scale").setValue([a.scale, a.scale]);
            tr.property("ADBE Vector Repeater Rotation").setValue(a.rotation);
            tr.property("ADBE Vector Repeater Opacity 2").setValue(a.endOpacity);
            return {layer: l.name, repeater: rep.name, copies: a.copies};
        },

        add_paragraph: function (a) {
            var c = findComp(a.comp), l = c.layers.addBoxText([a.box[0], a.box[1]], a.text);
            if (a.name) {
                l.name = a.name;
            }
            return layerInfo(l);
        },

        set_motion_blur: function (a) {
            var c = findComp(a.comp), i, l, n = 0;
            c.motionBlur = a.on;
            if (a.shutterAngle !== undefined) {
                c.shutterAngle = a.shutterAngle;
            }
            if (a.shutterPhase !== undefined) {
                c.shutterPhase = a.shutterPhase;
            }
            if (a.layers !== undefined) {
                for (i = 1; i <= c.numLayers; i++) {
                    l = c.layer(i);
                    if (l instanceof CameraLayer || l instanceof LightLayer) {
                        continue;
                    }
                    if (a.layers === "all" || contains(a.layers, l.name) || contains(a.layers, l.index)) {
                        l.motionBlur = a.on;
                        n += 1;
                    }
                }
            }
            return {comp: c.name, motionBlur: c.motionBlur, shutterAngle: c.shutterAngle, shutterPhase: c.shutterPhase,
                layers: n};
        },

        audio_source: function (a) {
            var l = findLayer(findComp(a.comp), a.layer);
            if (!l.hasAudio || !l.source || !l.source.file) {
                fail(l.name + " is not a layer with an audio file");
            }
            return {file: l.source.file.fsName, startTime: l.startTime, inPoint: l.inPoint, outPoint: l.outPoint};
        },

        add_markers: function (a) {
            var c = findComp(a.comp), prop = c.markerProperty, i, mv;
            for (i = 0; i < a.times.length; i++) {
                mv = new MarkerValue(a.comment);
                prop.setValueAtTime(a.times[i], mv);
            }
            return {comp: c.name, added: a.times.length, markers: prop.numKeys};
        },

        sequence_to_markers: function (a) {
            var c = findComp(a.comp), prop = c.markerProperty, times = [], i, l, out = [], end;
            for (i = 1; i <= prop.numKeys; i++) {
                times.push(prop.keyTime(i));
            }
            if (times.length < a.layers.length) {
                fail(c.name + " has " + times.length + " markers for " + a.layers.length + " layers");
            }
            for (i = 0; i < a.layers.length; i++) {
                l = findLayer(c, a.layers[i]);
                l.startTime += times[i] - l.inPoint;
                if (a.trim && i + 1 < times.length && l.outPoint > times[i + 1]) {
                    l.outPoint = times[i + 1];  // cut on the next beat
                }
                end = l.outPoint;
                out.push({layer: l.name, inPoint: round(l.inPoint), outPoint: round(end)});
            }
            return {layers: out};
        },

        reduce_project: function (a) {
            var keep = [], i, before = app.project.numItems, removed;
            for (i = 0; i < a.comps.length; i++) {
                keep.push(findComp(a.comps[i]));
            }
            removed = app.project.reduceProject(keep);
            return {kept: a.comps, removed: removed, items: [before, app.project.numItems]};
        },

        render_queue_list: function () {
            var rq = app.project.renderQueue, out = [], i, it, om;
            for (i = 1; i <= rq.numItems; i++) {
                it = rq.item(i);
                om = it.outputModule(1);
                out.push({index: i, comp: it.comp.name, status: statusName(it.status),
                    output: om && om.file ? om.file.fsName : null});
            }
            return out;
        },

        clear_render_queue: function (a) {
            var rq = app.project.renderQueue, i, it, removed = [];
            for (i = rq.numItems; i >= 1; i--) {
                it = rq.item(i);
                if (it.status === RQItemStatus.RENDERING) {
                    continue;
                }
                if (a.all || it.status === RQItemStatus.DONE) {
                    removed.push(it.comp.name);
                    it.remove();
                }
            }
            return {removed: removed.length, left: rq.numItems};
        },

        run_jsx: function (a) {
            return plain(eval(a.code));
        }
    };

    var READ_ONLY = {status: 1, list_items: 1, comp_info: 1, get_property: 1, list_effects: 1, property_tree: 1,
        missing_footage: 1, audio_source: 1, render_queue_list: 1};

    function run(name, args) {
        var result, cmd = commands[name];
        if (!cmd) {
            return stringify({error: "unknown command: " + name});
        }
        if (!READ_ONLY[name]) {
            app.beginUndoGroup("aemcp: " + name);
        }
        try {
            result = stringify({ok: cmd(args || {})});
        } catch (e) {
            result = stringify({error: e.message || String(e), line: e.line || null});
        } finally {
            if (!READ_ONLY[name]) {
                app.endUndoGroup();
            }
        }
        return result;
    }

    return {run: run, stringify: stringify, commands: commands};
}());
