import { NextRequest, NextResponse } from 'next/server'
import { getDb } from '@/lib/db'

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ symbol: string }> }
) {
  const { symbol } = await params

  // 從 DB 取股票名稱，用名稱搜尋效果較好
  const db = getDb()
  const row = db.prepare('SELECT name FROM stocks WHERE symbol = ?').get(symbol) as { name: string } | undefined
  const query = row?.name || symbol.replace('.TW', '').replace('.TWO', '')

  const url = `https://news.google.com/rss/search?q=${encodeURIComponent(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant`

  const res = await fetch(url, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    },
    next: { revalidate: 1800 },
  })

  if (!res.ok) return NextResponse.json({ items: [] })

  const xml = await res.text()
  const items: { title: string; link: string; pubDate: string; source: string }[] = []

  const itemBlocks = xml.match(/<item>([\s\S]*?)<\/item>/g) || []
  for (const block of itemBlocks.slice(0, 10)) {
    const title = (block.match(/<title><!\[CDATA\[(.*?)\]\]><\/title>/)?.[1]
      || block.match(/<title>(.*?)<\/title>/)?.[1] || '').trim()
    const link = block.match(/<link>(.*?)<\/link>/)?.[1]
      || block.match(/<guid[^>]*>(.*?)<\/guid>/)?.[1] || ''
    const pubDate = block.match(/<pubDate>(.*?)<\/pubDate>/)?.[1] || ''
    const source = block.match(/<source[^>]*>(.*?)<\/source>/)?.[1]
      || title.split(' - ').at(-1) || 'Google 新聞'
    // 移除 title 結尾的 " - 來源名稱"
    const cleanTitle = title.replace(/ - [^-]+$/, '')
    if (cleanTitle) items.push({ title: cleanTitle, link, pubDate, source })
  }

  return NextResponse.json({ items })
}
