# Polytesting Bot (poly-bot-web)

Bot Polymarket BTC 5 menit dengan web UI lokal. Backend Python membaca market Polymarket CLOB, harga BTC dari Binance, dan menyajikan dashboard + kontrol trading lewat browser.

**Ringkas Fitur**
- Web UI lokal dengan grafik, posisi, PnL, dan log.
- Market buy/limit buy, limit sell, cashout, cancel order.
- Guardrails untuk eksekusi (min size, slippage, anti-double-click).
- Auto-switch ke market 5m berikutnya dan prefetch token.
- Opsi autentikasi UI berbasis user/password.

**Struktur Folder**
- `polybot_web.py` backend utama (HTTP server + bot).
- `webui/` UI statis (HTML/CSS/JS) yang disajikan oleh server.
- `run_web.sh` launcher memakai `venv` lokal.
- `.env.example` template konfigurasi.

## Requirements
- Linux / WSL
- Python 3.10+
- Wallet Polymarket CLOB yang sudah funded
- Akses jaringan ke Polymarket CLOB HTTP + WebSocket
- Akses jaringan ke Binance HTTP (candlestick)
- Akses jaringan ke CDN `lightweight-charts` (untuk grafik UI)

## Quick Start
1. Clone repo dan masuk folder.
2. Buat virtualenv dan install deps.
3. Salin `.env.example` ke `.env` dan isi nilai rahasia.
4. Jalankan `./run_web.sh`.
5. Buka `http://127.0.0.1:8787`.

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install requests websocket-client python-dotenv py-clob-client

cp .env.example .env
./run_web.sh
```

## Konfigurasi `.env`
Isi semua **Required secrets** sebelum run. File `.env.example` menjadi referensi awal.

**Required secrets**
- `PK` private key wallet
- `WALLET_ADDRESS` address wallet
- `POLY_PROXY` proxy address (sesuai setup Polymarket)
- `CLOB_FUNDER` funder address (fallback ke `POLY_PROXY` atau `WALLET_ADDRESS` bila kosong)

**Core**
| Key | Default | Fungsi |
| --- | --- | --- |
| `CLOB_SIGNATURE_TYPE` | `2` | Tipe signature untuk CLOB client. |
| `DRY_RUN` | `1` | Mode simulasi, tidak kirim order. |
| `WEB_HOST` | `127.0.0.1` | Bind server web. |
| `WEB_PORT` | `8787` | Port server web. |

**Trading guards**
| Key | Default | Fungsi |
| --- | --- | --- |
| `MAX_ENTRY_CENT` | `99` | Batasi entry price (cent). |
| `MIN_MARKET_TIME_LEFT` | `45` | Minimum sisa waktu market untuk entry. |
| `ENTRY_SLIPPAGE_BPS` | `50` | Slippage entry (bps). |
| `EXIT_SLIPPAGE_BPS` | `80` | Slippage exit (bps). |
| `MIN_ORDER_SHARES` | `0.01` | Ukuran minimal order (shares). |
| `MIN_ORDER_USD` | `0.01` | Ukuran minimal order (USD). |
| `STRICT_EXECUTION` | `1` | Batasi attempt eksekusi real (lebih konservatif). |
| `BUY_CMD_GUARD_SEC` | `1.2` | Cooldown anti double-buy command. |
| `MARKET_BUY_ORDER_TYPE` | `FAK` | `FAK` atau `FOK` untuk market buy. |

**Position sync**
| Key | Default | Fungsi |
| --- | --- | --- |
| `POSITION_SYNC_GRACE` | `8` | Grace period sync posisi agar cepat update. |
| `POSITION_DUST_SHARES` | `0.005` | Threshold posisi kecil (shares). |
| `POSITION_DUST_USD` | `0.02` | Threshold posisi kecil (USD). |

**Chart & probabilitas**
| Key | Default | Fungsi |
| --- | --- | --- |
| `CHART_SAMPLE_SEC` | `1.0` | Interval sampling chart (detik). |
| `CHART_MAX_CANDLES_1M` | `360` | Batas jumlah candle 1m. |
| `PROB_VOL_WINDOW_SEC` | `240` | Window estimasi volatilitas. |
| `PROB_DEFAULT_VOL_ANNUAL` | `0.75` | Volatilitas default jika data minim. |
| `PROB_W_MARKET` | `0.50` | Bobot probabilitas dari market. |
| `PROB_W_PTB` | `0.40` | Bobot probabilitas dari PTB. |
| `PROB_W_MICRO` | `0.10` | Bobot probabilitas micro-signal. |
| `PROB_SCORE_DRIFT_SEC` | `15` | Toleransi drift untuk skor probabilitas. |

**PTB & switching**
| Key | Default | Fungsi |
| --- | --- | --- |
| `PTB_MAX_DRIFT_SEC` | `1` | Maks drift timestamp PTB sebelum dianggap stale. |
| `PTB_WEB_FALLBACK` | `0` | Fallback PTB via web (lebih longgar). |
| `PTB_WEB_RETRY_SEC` | `30` | Retry interval fallback PTB. |
| `NEXT_PREFETCH_SEC` | `120` | Lead time prefetch market berikutnya. |
| `SWITCH_MIN_REMAINING_SEC` | `10` | Minimum sisa detik sebelum switch target. |

**Binance HTTP**
| Key | Default | Fungsi |
| --- | --- | --- |
| `BINANCE_FORCE_IPV4` | `1` | Paksa IPv4 untuk request. |
| `BINANCE_HTTP_TIMEOUT` | `10` | Timeout request (detik). |
| `BINANCE_HTTP_PROXY` | kosong | Proxy HTTP. |
| `BINANCE_HTTPS_PROXY` | kosong | Proxy HTTPS. |
| `BINANCE_API_BASES` | list | Base URL Binance fallback. |

**Web auth (opsional)**
| Key | Default | Fungsi |
| --- | --- | --- |
| `WEB_USER` | kosong | Username login UI. |
| `WEB_PASS` | kosong | Password login UI. |
| `WEB_AUTH_TTL_SEC` | `3600` | TTL sesi login. |

**Catatan .env.example**
`.env.example` memuat beberapa key legacy yang belum dipakai oleh `polybot_web.py` saat ini (misal `BUY_FAIL_FAST`, `PTB_EXECUTION_WORKER`). Anda bisa mengabaikannya.

## Menjalankan
- Jalankan: `./run_web.sh` atau `python3 polybot_web.py`
- Buka UI: `http://127.0.0.1:8787`

**Mode DRY vs REAL**
- Default `DRY_RUN=1` (simulasi, tidak kirim order).
- Tombol `DRY/REAL` di UI bisa toggle runtime.

## Panduan UI
**Topbar**
- `SLUG` market 5m saat ini.
- `T` sisa waktu interval.
- `WS` status WebSocket.
- `MODE` DRY/REAL.
- `BAL` balance.
- `SESSION` PnL sesi.
- `WR` win rate.

**Target Market**
- `CURRENT` untuk trading interval sekarang.
- `NEXT` untuk order di market berikutnya.
- `PREVIOUS` read-only (lihat data market sebelumnya).

**Order Panel**
- `BUY UP/DOWN` market buy (USD sesuai input).
- `BUY NEXT` untuk target market berikutnya.
- `LIMIT BUY` masukkan `price (cent)` + `USD`.
- `CASH OUT` menjual posisi di target market aktif.
- `PREV` cash out posisi yang sudah bergeser ke market sebelumnya.
- `LIMIT SELL` masukkan `price (cent)` dan jumlah shares auto dari posisi.

**Log & Open Orders**
- Tab `SYSTEM` dan `TRADE` memisahkan log.
- Tombol `COPY` menyalin log.
- Open Orders menampilkan order aktif + tombol cancel per order.

## Command Manual (Input Box)
Anda bisa kirim perintah langsung via input box.

**Trading**
- `bu <usd>` buy UP market.
- `bd <usd>` buy DOWN market.
- `bnu <usd>` buy UP market berikutnya.
- `bnd <usd>` buy DOWN market berikutnya.
- `lu <cent> <usd>` limit buy UP.
- `ld <cent> <usd>` limit buy DOWN.
- `su <cent> [shares]` limit sell UP (shares opsional).
- `sd <cent> [shares]` limit sell DOWN (shares opsional).
- `cu` cash out UP.
- `cd` cash out DOWN.
- `cpu` cash out UP otomatis (termasuk posisi off-market).
- `cpd` cash out DOWN otomatis (termasuk posisi off-market).
- `ca` cancel all open orders.
- `co <order_id>` cancel order tertentu.

**System**
- `dry on` atau `dry off` toggle DRY/REAL.
- `target current|next|previous` set target market.
- `r` force refresh token/market.
- `q` stop bot.

## Keamanan & Akses
- API `state/chart/cmd` hanya menerima request dari client lokal.
- Jangan set `WEB_HOST=0.0.0.0` kecuali paham risikonya.
- Jika butuh akses remote, gunakan SSH port forwarding dan tetap bind ke `127.0.0.1`.
- Aktifkan `WEB_USER/WEB_PASS` untuk login UI.
- Jangan pernah commit `.env`.

## Troubleshooting
**Auth failed**
- Cek `PK`, `WALLET_ADDRESS`, `POLY_PROXY`, `CLOB_FUNDER`.
- Pastikan `CLOB_SIGNATURE_TYPE` cocok dengan setup wallet.

**Address already in use**
- Ganti `WEB_PORT` atau matikan proses lama.

**WebSocket reconnect loop**
- Cek jaringan.
- Pastikan endpoint Polymarket bisa diakses.

**PTB lambat muncul**
- Tunggu beberapa detik setelah switch.
- Jika sering kosong, pertimbangkan `PTB_WEB_FALLBACK=1`.

## Dev Notes
- Frontend ada di `webui/` dan di-serve statis.
- Jika `webui/index.html` tidak terbaca, server akan fallback ke HTML built-in.
- `run_web.sh` mengasumsikan venv di `./venv`.
