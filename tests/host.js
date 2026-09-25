// Test host: runs the scripts aftereffects_mcp.py sends against the fake After Effects, one JSON request per line
// on stdin, one JSON reply per line on stdout. The script context loses the ES5 built-ins ExtendScript lacks, so
// library code that would fail inside After Effects fails here too.
"use strict";
const vm = require("vm");
const readline = require("readline");
const {makeAE} = require("./fake_ae");

const ctx = vm.createContext({});
const ae = makeAE(vm.runInContext("Array", ctx));
Object.assign(ctx, ae.globals);
vm.runInContext(`
    delete Array.prototype.map; delete Array.prototype.forEach; delete Array.prototype.filter;
    delete Array.prototype.indexOf; delete Array.prototype.reduce; delete Array.prototype.some;
    delete Array.prototype.every; delete Array.prototype.find; delete Array.isArray;
    delete String.prototype.trim; delete Object.keys; JSON = undefined;
`, ctx);

readline.createInterface({input: process.stdin}).on("line", (line) => {
    const req = JSON.parse(line);
    let reply;
    try {
        if (req.script !== undefined) {
            reply = {result: vm.runInContext(req.script, ctx)};
        } else {
            // inspection from the tests, in the host's own realm
            reply = {result: new Function("ae", "return (" + req.inspect + ");")(ae)};
        }
    } catch (e) {
        reply = {exception: String((e && e.message) || e)};
    }
    process.stdout.write(JSON.stringify(reply) + "\n");
});
