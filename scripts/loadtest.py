#!/usr/bin/env python3
"""Creator OS load-test harness (M6).

Drives real HTTP against a running backend and reports throughput, latency
percentiles and error rates. Written for M6 Phase 4; the measured results it
produced are recorded in docs/M6_VALIDATION_REPORT.md.

It boots the server itself under two connection-pool configurations so the
before/after comparison is apples-to-apples on one machine:

    M5 defaults  pool 5+10=15
    M6 defaults  pool 20+60=80

Prerequisites:
  * a reachable PostgreSQL, and /home/user/prod.env style env file
  * an existing admin user matching ADMIN_USER / ADMIN_PASSWORD below

Usage:
    python scripts/loadtest.py

Results are written to logs/benchmark.json.

This is an operator/CI tool, not a unit test: a full run takes ~2 minutes and
needs a live database. The permanent assertions extracted from these findings
live in backend/tests/m6/test_db_pool_capacity_m6.py.
"""
import asyncio, os, subprocess, time, httpx, collections, signal, json, statistics
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
BACKEND = str(REPO / "backend")
BASE = os.environ.get("LOADTEST_BASE_URL", "http://127.0.0.1:8099")
ENV_FILE = os.environ.get("LOADTEST_ENV_FILE", "/home/user/prod.env")
ADMIN_USER = os.environ.get("LOADTEST_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("LOADTEST_ADMIN_PASSWORD", "Adm1n-Str0ng-Pass!2026")
LOG_DIR = pathlib.Path(os.environ.get("LOADTEST_LOG_DIR", str(REPO.parent / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)

def start(extra, log):
    env=dict(os.environ)
    for l in open(ENV_FILE):
        l=l.strip()
        if l and not l.startswith("#") and "=" in l: k,v=l.split("=",1); env[k]=v
    env.update(extra); env["PYTHONPATH"]=BACKEND
    p=subprocess.Popen(["/home/user/.venv/bin/uvicorn","app.main:app","--host","127.0.0.1","--port","8099","--workers","1"],
                       cwd=BACKEND, env=env, stdout=open(log,"w"), stderr=subprocess.STDOUT)
    for _ in range(160):
        time.sleep(0.5)
        try:
            if httpx.get(f"{BASE}/health/live",timeout=2).status_code==200: return p
        except Exception: pass
    raise RuntimeError("nostart")

async def scenario(path, conc, per, auth=True, tag=""):
    hdr={}
    if auth:
        async with httpx.AsyncClient() as c:
            hdr={"Authorization":"Bearer "+(await c.post(f"{BASE}/api/auth/login",
                json={"username":ADMIN_USER,"password":ADMIN_PASSWORD})).json()["access_token"]}
    codes=collections.Counter(); lat=[]
    lim=httpx.Limits(max_connections=conc+40,max_keepalive_connections=conc+40)
    t0=time.perf_counter()
    async with httpx.AsyncClient(limits=lim,timeout=120) as c:
        async def one():
            for _ in range(per):
                t=time.perf_counter()
                try:
                    r=await c.get(BASE+path,headers=hdr)
                    codes[r.status_code]+=1; lat.append((time.perf_counter()-t)*1000)
                except Exception as e: codes[type(e).__name__]+=1
        await asyncio.gather(*[one() for _ in range(conc)])
    d=time.perf_counter()-t0; lat.sort(); n=len(lat)
    def q(x): return round(lat[min(int(n*x),n-1)],1) if n else None
    tot=sum(codes.values()); ok=codes.get(200,0)
    return {"scenario":tag,"path":path,"conc":conc,"requests":tot,"ok":ok,
            "errors":tot-ok,"error_rate_pct":round(100*(tot-ok)/tot,1) if tot else 0,
            "rps":round(tot/d,1),"wall_s":round(d,2),
            "p50_ms":q(.5),"p95_ms":q(.95),"p99_ms":q(.99),
            "max_ms":round(lat[-1],1) if lat else None,"codes":dict(codes)}

CONFIGS={
 "M5 defaults (pool 5+10=15)": {"DB_POOL_SIZE":"5","DB_MAX_OVERFLOW":"10","DB_POOL_TIMEOUT_SECONDS":"30"},
 "M6 defaults (pool 20+60=80)":{"DB_POOL_SIZE":"20","DB_MAX_OVERFLOW":"60","DB_POOL_TIMEOUT_SECONDS":"10"},
}
async def main():
    allr={}
    for label,extra in CONFIGS.items():
        p=start(extra, str(LOG_DIR / f"bench_{extra['DB_POOL_SIZE']}.log"))
        rows=[]
        try:
            rows.append(await scenario("/health/live",50,10,False,"health (no DB)"))
            await asyncio.sleep(1)
            for conc in (10,50,100):
                rows.append(await scenario("/api/workflows/",conc,5,True,f"authed+DB conc={conc}"))
                await asyncio.sleep(2)
        finally:
            p.send_signal(signal.SIGTERM); p.wait(timeout=40); time.sleep(2)
        allr[label]=rows
        print(f"\n### {label}")
        for r in rows:
            print(f"  {r['scenario']:22} {r['ok']:4}/{r['requests']:4} ok  err={r['error_rate_pct']:5.1f}%  "
                  f"{r['rps']:7.1f} rps  p50={r['p50_ms']:8} p95={r['p95_ms']:9} p99={r['p99_ms']:9}")
    json.dump(allr, open(LOG_DIR / "benchmark.json","w"), indent=1)
asyncio.run(main())
