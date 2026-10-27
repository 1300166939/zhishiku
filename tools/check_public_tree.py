"""Offline release checks; reports file locations without matched values."""
import ast,ipaddress,json,re
from pathlib import Path
from urllib.parse import urlparse
ROOT=Path(__file__).resolve().parents[1]
PATTERNS={
 'private-key':r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
 'github-token':r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})',
 'aws-access-key':r'(?:AKIA|ASIA)[A-Z0-9]{16}',
 'credential-token':r'kb_[A-Za-z0-9_-]{43,}',
 'personal-local-path':r'/[U]sers/[^\s\"\']+',
}
issues=[];checked=0
for f in sorted(ROOT.rglob('*')):
 if not f.is_file() or any(p in {'.git','__pycache__','.venv'} for p in f.relative_to(ROOT).parts):continue
 rel=f.relative_to(ROOT).as_posix();checked+=1
 if f.name.endswith('.env') or f.name=='.env':issues.append({'file':rel,'rule':'runtime-env'})
 if any(x in f.name for x in ['.private.','.sqlite','.pem']) or f.suffix in {'.db','.key','.zip','.log'}:issues.append({'file':rel,'rule':'runtime-artifact'})
 try:t=f.read_text()
 except UnicodeDecodeError:issues.append({'file':rel,'rule':'unexpected-binary'});continue
 for rule,pat in PATTERNS.items():
  for m in re.finditer(pat,t):issues.append({'file':rel,'line':t.count('\n',0,m.start())+1,'rule':rule})
 if f.suffix=='.py':
  try:tree=ast.parse(t)
  except SyntaxError:issues.append({'file':rel,'rule':'syntax'});continue
  for n in ast.walk(tree):
   if not isinstance(n,ast.Constant) or not isinstance(n.value,str):continue
   for u in re.findall(r'https?://[^\s\"\'<>]+',n.value):
    try:host=urlparse(u).hostname
    except ValueError:host=None
    if host and not (host in {'ark.cn-beijing.volces.com','127.0.0.1','localhost','example.com'} or host.endswith('.test')):issues.append({'file':rel,'line':n.lineno,'rule':'non-example-endpoint'})
   for m in re.finditer(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',n.value):
    try:ip=ipaddress.ip_address(m.group())
    except ValueError:continue
    if not ip.is_loopback:issues.append({'file':rel,'line':n.lineno,'rule':'non-loopback-ip'})
print(json.dumps({'status':'FAIL' if issues else 'PASS','checked_files':checked,'issues':issues},indent=2))
raise SystemExit(bool(issues))
