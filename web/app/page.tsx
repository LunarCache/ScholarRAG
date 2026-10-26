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
  return (
    <main className="shell">
      <section style={{
        margin: "auto",
        padding: "36px 32px",
        maxWidth: 480,
        width: "calc(100% - 32px)",
        background: "var(--bg-surface)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-lg)",
        boxShadow: "var(--shadow-lg)",
        textAlign: "center",
      }}>
        <div style={{ display: "inline-grid", placeItems: "center", width: 44, height: 44, borderRadius: 12, background: "linear-gradient(135deg, var(--brand-primary), var(--brand-hover))", color: "#fff", fontWeight: 700, fontSize: 20, marginBottom: 14 }}>S</div>
        <h1 style={{ margin: "0 0 10px", fontSize: 22 }}>ScholarRAG</h1>
        {error ? (
          <>
            <p role="alert" style={{ color: "var(--status-danger)", background: "var(--status-danger-bg)", padding: "12px 14px", borderRadius: "var(--radius-sm)", fontSize: 13 }}>
              无法连接 API 或确认运行模式，请检查服务地址后重试。
            </p>
            <div style={{ display: "flex", justifyContent: "center", gap: 12, marginTop: 18 }}>
              <button className="upload" onClick={() => setAttempt((value) => value + 1)}>重试连接</button>
              <Link href="/workspace" style={{ display: "inline-flex", alignItems: "center", padding: "0 14px", height: 32, borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)", color: "var(--text-primary)", textDecoration: "none", fontSize: 12, fontWeight: 600 }}>
                进入工作空间登录
              </Link>
            </div>
          </>
        ) : (
          <p role="status" style={{ color: "var(--text-secondary)", fontSize: 13.5 }}>正在连接研究工作台…</p>
        )}
      </section>
    </main>
  );
}
