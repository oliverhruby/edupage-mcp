import json
import sys

sys.path.insert(0, r"C:\Users\oliver.hruby\Source\Personal\edupage-mcp\src")

import edupage_mcp as m

if not m._clients:
    print("LOGIN", m.login_all())

day = sys.argv[1] if len(sys.argv) > 1 else "2026-09-09"
name = sys.argv[2] if len(sys.argv) > 2 else None
sub = sys.argv[3] if len(sys.argv) > 3 else None

result = m.get_day_summary(date_str=day, name=name, subdomain=sub)
print(json.dumps(result, indent=2, ensure_ascii=False, default=str))