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
            return {text: v.text, font: v.font, fontSize: v.fontSize, fillColor: plain(v.fillColor)};
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
            var l = findLayer(findComp(a.comp), a.layer), p = findProperty(l, a.property), i, k, idx, dims, eases,
                low, j, kind, influence, times = [];
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
                p.setValueAtTime(k.time, toValue(p, k.value));
                idx = p.nearestKeyIndex(k.time);
                if (kind === "hold") {
                    p.setInterpolationTypeAtKey(idx, KeyframeInterpolationType.HOLD, KeyframeInterpolationType.HOLD);
                } else if (kind === "linear") {
                    p.setInterpolationTypeAtKey(idx, KeyframeInterpolationType.LINEAR, KeyframeInterpolationType.LINEAR);
                } else if (p.propertyValueType !== PropertyValueType.TEXT_DOCUMENT) {
                    influence = k.influence || 33.33;
                    // one ease per dimension, except spatial properties (position) which take a single one
                    dims = (p.value instanceof Array && !p.isSpatial) ? p.value.length : 1;
                    eases = [];
                    low = [];
                    for (j = 0; j < dims; j++) {
                        eases.push(new KeyframeEase(0, influence));
                        low.push(new KeyframeEase(0, 0.1));  // 0.1 is the smallest influence: no easing
                    }
                    p.setInterpolationTypeAtKey(idx, KeyframeInterpolationType.BEZIER, KeyframeInterpolationType.BEZIER);
                    // ease_in slows into the key (incoming side), ease_out slows out of it (outgoing side)
                    p.setTemporalEaseAtKey(idx, kind === "ease_out" ? low : eases, kind === "ease_in" ? low : eases);
                }
                times.push(round(p.keyTime(idx)));
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

        run_jsx: function (a) {
            return plain(eval(a.code));
        }
    };

    var READ_ONLY = {status: 1, list_items: 1, comp_info: 1, get_property: 1, list_effects: 1};

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
