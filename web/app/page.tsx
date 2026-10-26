"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import LocalWorkbench from "./LocalWorkbench";

const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export default function Home() {
  const router = useRouter();
  const [local, setLocal] = useState(false);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const timer = setTimeout(() => controller.abort(), 10000);
    setError(false);
    void fetch(`${API}/api/health`, { signal: controller.signal, cache: "no-store", credentials: "omit", redirect: "error" })
      .then(async (response) => {
        if (!response.ok) throw new Error("API unavailable");
        const mode = (await response.json()).auth_mode;
        if (!active) return;
        if (mode === "session") router.replace("/workspace");
        else if (mode === "local") setLocal(true);
        else setError(true);
      }).catch(() => { if (active) setError(true); })
      .finally(() => clearTimeout(timer));
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [router, attempt]);
  if (local) return <LocalWorkbench />;
  return <main className="shell"><section style={{ margin: "auto", padding: 24, maxWidth: 480 }}>
    <h1>ScholarRAG</h1>
    {error ? <><p role="alert">无法连接 API 或确认运行模式，请检查服务地址后重试。</p><button onClick={() => setAttempt((value) => value + 1)}>重试连接</button><p><Link href="/workspace">进入工作空间登录</Link></p></> : <p role="status">正在连接研究工作台…</p>}
  </section></main>;
}
