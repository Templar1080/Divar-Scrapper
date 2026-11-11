import json, re, shutil
from pathlib import Path
ROOT = Path(__file__).parent
three = Path("/tmp/three.min.js").read_text(encoding="utf-8")
data = (ROOT/"out/app_data.json").read_text(encoding="utf-8")
tpl = (ROOT/"app/template.html").read_text(encoding="utf-8")
out = tpl.replace("/*__THREE__*/", three).replace("/*__DATA__*/", data)
p = ROOT/"out/divar_intel.html"
p.write_text(out, encoding="utf-8")
print("built", p, round(p.stat().st_size/1e6,2), "MB")
