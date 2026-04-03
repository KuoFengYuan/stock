import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: '台股推薦系統',
  description: '基於 AI/ML 的台股每日推薦',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-Hant">
      <body className="min-h-screen bg-slate-900 text-slate-100">
        <nav className="border-b border-slate-700 px-6 py-3 flex items-center gap-6">
          <span className="font-bold text-lg text-white">台股推薦系統</span>
          <a href="/" className="text-slate-300 hover:text-white text-sm">推薦清單</a>
          <a href="/settings" className="text-slate-300 hover:text-white text-sm">篩選設定</a>
        </nav>
        <main className="px-6 py-6">{children}</main>
      </body>
    </html>
  )
}
