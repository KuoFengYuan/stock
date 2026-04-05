import { NextRequest, NextResponse } from 'next/server'

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ symbol: string }> }
) {
  const { symbol } = await params
  const ticker = symbol.replace('.TW', '.TW').replace('.TWO', '.TWO')
  const url = `https://feeds.finance.yahoo.com/rss/2.0/headline?s=${encodeURIComponent(ticker)}&region=TW&lang=zh-TW`

  const res = await fetch(url, {
    headers: { 'User-Agent': 'Mozilla/5.0' },
    next: { revalidate: 1800 },
  })

  if (!res.ok) return NextResponse.json({ items: [] })

  const xml = await res.text()
  const items: { title: string; link: string; pubDate: string; source: string }[] = []

  const itemBlocks = xml.match(/<item>([\s\S]*?)<\/item>/g) || []
  for (const block of itemBlocks.slice(0, 10)) {
    const title = block.match(/<title><!\[CDATA\[(.*?)\]\]><\/title>/)?.[1]
      || block.match(/<title>(.*?)<\/title>/)?.[1] || ''
    const link = block.match(/<link>(.*?)<\/link>/)?.[1]
      || block.match(/<guid[^>]*>(.*?)<\/guid>/)?.[1] || ''
    const pubDate = block.match(/<pubDate>(.*?)<\/pubDate>/)?.[1] || ''
    const source = block.match(/<source[^>]*>(.*?)<\/source>/)?.[1] || 'Yahoo Finance'
    if (title) items.push({ title, link, pubDate, source })
  }

  return NextResponse.json({ items })
}
