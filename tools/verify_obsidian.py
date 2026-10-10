"""Client-level link/render verification for the configured dedicated Vault."""
import json
from pathlib import Path
import subprocess
import time
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from journal_core import BASE, Registry, now, put_json, read_json
from publisher import entry_path


def main():
    reg = Registry()
    cli = r"D:\obsidian\Obsidian.com"
    def call(*args):
        p = subprocess.run([cli, "vault=" + reg.vault.name, *args], capture_output=True, encoding="utf-8", check=True)
        if "not enabled" in p.stdout or "unable to find" in p.stderr:
            raise RuntimeError(p.stdout + p.stderr)
        return p.stdout.strip()
    def evaluate(code):
        output = call("eval", "code=" + code)
        return json.loads(output.removeprefix("=> "))
    actual_path = call("vault", "info=path")
    if Path(actual_path).resolve() != reg.vault:
        raise RuntimeError("Obsidian target does not match configured Vault")
    def window_state():
        return evaluate('JSON.stringify((()=>{const w=require("@electron/remote").getCurrentWindow();return {visible:w.isVisible(),minimized:w.isMinimized()};})())')
    initial_window = window_state()
    if not initial_window["visible"] or initial_window["minimized"]:
        raise RuntimeError("Vault is loaded but its native window is hidden/minimized. Show the Obsidian window before client acceptance; screenshots alone do not prove visibility.")
    decision = read_json(BASE / "state/entries/engineering-journal/ADR-0001.json")
    call("open", "path=" + entry_path(reg, decision))
    time.sleep(0.6)
    call("dev:screenshot", "path=" + str(BASE / "docs/note-preview.png"))
    call("command", "id=graph:open")
    call("eval", "code=(()=>{const s='path:Engineering -path:Engineering/Templates -path:Engineering/Needs-Review';const p=app.internalPlugins.plugins.graph.instance;p.options.search=s;const f=app.workspace.getLeavesOfType('graph')[0].view.dataEngine.filterOptions.search;f.inputEl.value=s;f.inputEl.dispatchEvent(new Event('input',{bubbles:true}));p.saveOptions();app.workspace.leftSplit.collapse();})()")
    for _ in range(10):
        time.sleep(0.4)
        result = evaluate("JSON.stringify({path:app.vault.adapter.getBasePath(),notes:app.vault.getMarkdownFiles().length,unresolved:Object.values(app.metadataCache.unresolvedLinks).reduce((n,x)=>n+Object.keys(x).length,0),graph:app.workspace.getLeavesOfType('graph').map(l=>({nodes:l.view.renderer.nodes.length,links:l.view.renderer.links.length,labels:l.view.renderer.nodes.map(n=>n.id)}))})")
        if result["graph"] and result["graph"][0]["nodes"] > 0:
            break
    if result["unresolved"] or not result["graph"][0]["nodes"]:
        raise RuntimeError(result)
    call("dev:cdp", "method=Input.dispatchMouseEvent", 'params={"type":"mouseMoved","x":20,"y":20}')
    time.sleep(0.5)
    call("dev:screenshot", "path=" + str(BASE / "docs/graph-preview.png"))
    errors = call("dev:errors")
    final_window = window_state()
    if not final_window["visible"] or final_window["minimized"]:
        raise RuntimeError("Obsidian window became hidden/minimized during acceptance")
    report = {"timestamp": now(), "version": call("version"), "result": result, "errors": errors,
              "window": {"initial": initial_window, "final": final_window}}
    put_json(BASE / "docs/obsidian-runtime.json", report)
    print(json.dumps({"notes": result["notes"], "unresolved": result["unresolved"], "graph": [{k: v for k, v in x.items() if k != "labels"} for x in result["graph"]], "errors": errors, "window": report["window"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
