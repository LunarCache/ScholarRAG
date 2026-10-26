import type { Metadata } from "next";
import "katex/dist/katex.min.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "ScholarRAG — 可追溯多模态学术论文工作台",
  description: "Traceable multimodal Agentic RAG workbench for reading, comparing, and evaluating academic papers",
  other: {
    "color-scheme": "light dark",
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}
