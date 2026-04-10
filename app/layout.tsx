import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: '台股推薦系統',
  description: '基於 AI/ML 的台股每日推薦',
  viewport: 'width=device-width, initial-scale=1',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-Hant">
      <body className="min-h-screen bg-slate-900 text-slate-100">
        <nav className="border-b border-slate-800 px-4 py-2 flex items-center gap-5">
          <a href="/" className="font-bold text-sm text-white hover:text-slate-200">台股推薦系統</a>
          <a href="/" className="text-slate-400 hover:text-white text-xs">推薦清單</a>
          <a href="/settings" className="text-slate-400 hover:text-white text-xs">篩選設定</a>
        </nav>
        <main className="px-3 py-2 sm:px-4 sm:py-3 overflow-x-hidden">{children}</main>
      </body>
    </html>
  )
}
