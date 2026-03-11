use std::env;
use std::net::SocketAddr;
use std::str::FromStr as _;
use std::sync::Arc;

use alloy::signers::Signer as _;
use alloy::signers::local::PrivateKeySigner;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use futures::StreamExt as _;
use polymarket_client_sdk::POLYGON;
use polymarket_client_sdk::auth::Normal;
use polymarket_client_sdk::auth::state::{Authenticated, Unauthenticated};
use polymarket_client_sdk::clob::types::{Amount, OrderType, Side, SignatureType};
use polymarket_client_sdk::clob::ws::Client as WsClient;
use polymarket_client_sdk::clob::ws::types::response::WsMessage;
use polymarket_client_sdk::clob::{Client as ClobClient, Config as ClobConfig};
use polymarket_client_sdk::types::{Address, Decimal, U256};
use serde::{Deserialize, Serialize};
use tokio::net::TcpListener;
use tokio::sync::Mutex;
use tower_http::cors::CorsLayer;
use tracing::{info, warn};
use tracing_subscriber::EnvFilter;

type SharedState = Arc<AppState>;
type AuthedClobClient = ClobClient<Authenticated<Normal>>;
type PublicWsClient = WsClient<Unauthenticated>;
type AuthedWsClient = WsClient<Authenticated<Normal>>;

#[derive(Clone)]
struct AppState {
    clob: AuthedClobClient,
    ws_public: PublicWsClient,
    ws_user: AuthedWsClient,
    signer: PrivateKeySigner,
    quotes: Arc<dashmap::DashMap<String, QuoteState>>,
    watched: Arc<dashmap::DashMap<String, bool>>,
    recent_events: Arc<Mutex<Vec<String>>>,
}

#[derive(Debug, Clone, Serialize)]
struct QuoteState {
    token_id: String,
    market: String,
    best_bid: String,
    best_ask: String,
    spread: String,
    timestamp_ms: i64,
}

#[derive(Debug, Serialize)]
struct HealthResponse {
    ok: bool,
}

#[derive(Debug, Serialize)]
struct StateResponse {
    watched_assets: Vec<String>,
    quotes: Vec<QuoteState>,
    recent_events: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct WatchRequest {
    asset_ids: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct MarketOrderRequest {
    token_id: String,
    side: String,
    amount: String,
    amount_kind: Option<String>,
    price: Option<String>,
    order_type: Option<String>,
}

#[derive(Debug, Serialize)]
struct MarketOrderResponse {
    ok: bool,
    order_id: String,
    success: bool,
    status: String,
    making_amount: String,
    taking_amount: String,
    error_msg: Option<String>,
    trade_ids: Vec<String>,
    transaction_hashes: Vec<String>,
}

#[derive(Debug, Serialize)]
struct ErrorResponse {
    ok: bool,
    error: String,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    dotenvy::dotenv().ok();
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .init();

    let worker_host = env::var("PTB_WORKER_HOST").unwrap_or_else(|_| "127.0.0.1".to_owned());
    let worker_port = env::var("PTB_WORKER_PORT")
        .ok()
        .and_then(|v| v.parse::<u16>().ok())
        .unwrap_or(8788);

    let state = Arc::new(build_state().await?);

    spawn_user_stream(state.clone());

    let app = Router::new()
        .route("/health", get(health))
        .route("/api/state", get(state_snapshot))
        .route("/api/watch", post(watch_assets))
        .route("/api/order/market", post(post_market_order))
        .layer(CorsLayer::permissive())
        .with_state(state);

    let addr = SocketAddr::from_str(&format!("{worker_host}:{worker_port}"))?;
    let listener = TcpListener::bind(addr).await?;
    info!("execution worker listening on http://{addr}");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn build_state() -> anyhow::Result<AppState> {
    let private_key = env::var("PK")
        .or_else(|_| env::var("POLYMARKET_PRIVATE_KEY"))
        .map_err(|_| anyhow::anyhow!("missing PK or POLYMARKET_PRIVATE_KEY"))?;
    let clob_host =
        env::var("PTB_CLOB_HOST").unwrap_or_else(|_| "https://clob.polymarket.com".to_owned());
    let configured_sig = configured_signature_type();
    let configured_funder = configured_funder()?;

    let signer = PrivateKeySigner::from_str(&private_key)?.with_chain_id(Some(POLYGON));
    let base_client = ClobClient::new(
        &clob_host,
        ClobConfig::builder().use_server_time(true).build(),
    )?;

    let mut auth = base_client.authentication_builder(&signer);
    if let Some(signature_type) = configured_sig {
        auth = auth.signature_type(signature_type);
    }
    if let Some(funder) = configured_funder {
        auth = auth.funder(funder);
    }
    let clob = auth.authenticate().await?;
    info!(
        signer = %signer.address(),
        clob_host = %clob_host,
        configured_signature_type = ?configured_sig,
        configured_funder = ?configured_funder,
        auth_address = %clob.address(),
        "execution worker authenticated"
    );

    let ws_public = WsClient::default();
    let ws_user = WsClient::default().authenticate(clob.credentials().clone(), clob.address())?;

    Ok(AppState {
        clob,
        ws_public,
        ws_user,
        signer,
        quotes: Arc::new(dashmap::DashMap::new()),
        watched: Arc::new(dashmap::DashMap::new()),
        recent_events: Arc::new(Mutex::new(Vec::new())),
    })
}

fn configured_signature_type() -> Option<SignatureType> {
    let raw = env::var("PTB_WORKER_SIG_KIND")
        .ok()
        .or_else(|| env::var("CLOB_SIGNATURE_TYPE").ok())?;
    let normalized = raw.trim().to_ascii_lowercase();
    match normalized.as_str() {
        "0" | "eoa" => Some(SignatureType::Eoa),
        "1" | "proxy" | "magic" | "email" => Some(SignatureType::Proxy),
        "2" | "browser" | "browser_wallet" | "browser_proxy" | "safe" | "gnosis"
        | "gnosis_safe" => Some(SignatureType::GnosisSafe),
        other => {
            warn!("unknown signature type '{other}', falling back to default auth");
            None
        }
    }
}

fn configured_funder() -> anyhow::Result<Option<Address>> {
    for key in ["CLOB_FUNDER", "POLY_PROXY", "WALLET_ADDRESS"] {
        if let Ok(value) = env::var(key) {
            let trimmed = value.trim();
            if !trimmed.is_empty() {
                return Ok(Some(Address::from_str(trimmed)?));
            }
        }
    }
    Ok(None)
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse { ok: true })
}

async fn state_snapshot(State(state): State<SharedState>) -> Json<StateResponse> {
    let watched_assets = state
        .watched
        .iter()
        .map(|row| row.key().clone())
        .collect::<Vec<_>>();
    let quotes = state
        .quotes
        .iter()
        .map(|row| row.value().clone())
        .collect::<Vec<_>>();
    let recent_events = state.recent_events.lock().await.clone();
    Json(StateResponse {
        watched_assets,
        quotes,
        recent_events,
    })
}

async fn watch_assets(
    State(state): State<SharedState>,
    Json(req): Json<WatchRequest>,
) -> impl IntoResponse {
    let mut added = Vec::new();
    for raw in req.asset_ids {
        let token_s = raw.trim().to_owned();
        if token_s.is_empty() {
            continue;
        }
        if state.watched.insert(token_s.clone(), true).is_none() {
            added.push(token_s.clone());
            spawn_quote_stream(state.clone(), token_s);
        }
    }
    Json(serde_json::json!({ "ok": true, "added": added }))
}

async fn post_market_order(
    State(state): State<SharedState>,
    Json(req): Json<MarketOrderRequest>,
) -> impl IntoResponse {
    match do_market_order(state, req).await {
        Ok(resp) => (StatusCode::OK, Json(resp)).into_response(),
        Err(err) => (
            StatusCode::BAD_REQUEST,
            Json(ErrorResponse {
                ok: false,
                error: err.to_string(),
            }),
        )
            .into_response(),
    }
}

async fn do_market_order(
    state: SharedState,
    req: MarketOrderRequest,
) -> anyhow::Result<MarketOrderResponse> {
    let token_id = U256::from_str(req.token_id.trim())?;
    let side = parse_side(&req.side)?;
    let order_type = parse_order_type(req.order_type.as_deref());
    let amount_value = Decimal::from_str(req.amount.trim())?;
    let amount_kind = req.amount_kind.unwrap_or_else(|| "usdc".to_owned());
    let price = req
        .price
        .as_deref()
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map(Decimal::from_str)
        .transpose()?;

    let order = match side {
        Side::Buy => {
            let amount = match amount_kind.trim().to_ascii_lowercase().as_str() {
                "usd" | "usdc" => Amount::usdc(amount_value)?,
                "share" | "shares" => Amount::shares(amount_value)?,
                other => return Err(anyhow::anyhow!("unsupported amount_kind '{other}'")),
            };

            let mut builder = state
                .clob
                .market_order()
                .token_id(token_id)
                .side(side)
                .amount(amount)
                .order_type(order_type);

            if let Some(price) = price {
                builder = builder.price(price);
            }

            builder.build().await?
        }
        Side::Sell => {
            let size = match amount_kind.trim().to_ascii_lowercase().as_str() {
                "share" | "shares" => amount_value,
                "usd" | "usdc" => {
                    return Err(anyhow::anyhow!("sell orders require amount_kind 'shares'"));
                }
                other => return Err(anyhow::anyhow!("unsupported amount_kind '{other}'")),
            };
            let price = price.ok_or_else(|| anyhow::anyhow!("sell orders require price"))?;

            state
                .clob
                .limit_order()
                .token_id(token_id)
                .side(side)
                .size(size)
                .price(price)
                .order_type(order_type)
                .build()
                .await?
        }
        _ => return Err(anyhow::anyhow!("unsupported side")),
    };

    let signed = state.clob.sign(&state.signer, order).await?;
    let resp = state.clob.post_order(signed).await?;

    push_event(
        &state,
        format!(
            "POST {} token={} status={:?} success={} order_id={}",
            req.side.to_ascii_uppercase(),
            req.token_id,
            resp.status,
            resp.success,
            resp.order_id
        ),
    )
    .await;

    Ok(MarketOrderResponse {
        ok: true,
        order_id: resp.order_id,
        success: resp.success,
        status: format!("{:?}", resp.status),
        making_amount: resp.making_amount.to_string(),
        taking_amount: resp.taking_amount.to_string(),
        error_msg: resp.error_msg,
        trade_ids: resp.trade_ids,
        transaction_hashes: resp
            .transaction_hashes
            .into_iter()
            .map(|h| h.to_string())
            .collect(),
    })
}

fn spawn_quote_stream(state: SharedState, token_s: String) {
    tokio::spawn(async move {
        let token_id = match U256::from_str(&token_s) {
            Ok(v) => v,
            Err(err) => {
                push_event(&state, format!("WATCH invalid token {}: {}", token_s, err)).await;
                return;
            }
        };

        let mut attempt = 0u32;
        loop {
            attempt += 1;
            let stream = match state.ws_public.subscribe_best_bid_ask(vec![token_id]) {
                Ok(stream) => stream,
                Err(err) => {
                    push_event(
                        &state,
                        format!("WATCH subscribe failed {}: {}", token_s, err),
                    )
                    .await;
                    tokio::time::sleep(reconnect_delay(attempt)).await;
                    continue;
                }
            };
            let mut stream = Box::pin(stream);
            push_event(
                &state,
                format!("WATCH subscribed {} (attempt {})", token_s, attempt),
            )
            .await;

            let mut saw_message = false;
            while let Some(item) = stream.next().await {
                match item {
                    Ok(quote) => {
                        saw_message = true;
                        if attempt != 1 {
                            attempt = 1;
                        }
                        state.quotes.insert(
                            token_s.clone(),
                            QuoteState {
                                token_id: quote.asset_id.to_string(),
                                market: quote.market.to_string(),
                                best_bid: quote.best_bid.to_string(),
                                best_ask: quote.best_ask.to_string(),
                                spread: quote.spread.to_string(),
                                timestamp_ms: quote.timestamp,
                            },
                        );
                    }
                    Err(err) => {
                        push_event(&state, format!("WATCH stream error {}: {}", token_s, err))
                            .await;
                        break;
                    }
                }
            }

            if !state.watched.contains_key(&token_s) {
                push_event(&state, format!("WATCH stopped {}", token_s)).await;
                break;
            }

            let delay = reconnect_delay(attempt);
            let reason = if saw_message { "reconnect" } else { "retry" };
            push_event(
                &state,
                format!("WATCH {} {} in {}s", token_s, reason, delay.as_secs()),
            )
            .await;
            tokio::time::sleep(delay).await;
        }
    });
}

fn spawn_user_stream(state: SharedState) {
    tokio::spawn(async move {
        let mut attempt = 0u32;
        loop {
            attempt += 1;
            let stream = match state.ws_user.subscribe_user_events(Vec::new()) {
                Ok(stream) => stream,
                Err(err) => {
                    push_event(&state, format!("USER stream start failed: {}", err)).await;
                    tokio::time::sleep(reconnect_delay(attempt)).await;
                    continue;
                }
            };
            let mut stream = Box::pin(stream);
            push_event(
                &state,
                format!("USER stream subscribed (attempt {})", attempt),
            )
            .await;

            let mut saw_message = false;
            while let Some(item) = stream.next().await {
                match item {
                    Ok(WsMessage::Order(order)) => {
                        saw_message = true;
                        if attempt != 1 {
                            attempt = 1;
                        }
                        push_event(
                            &state,
                            format!(
                                "ORDER {} side={:?} status={:?} matched={}",
                                order.id,
                                order.side,
                                order.status,
                                order
                                    .size_matched
                                    .map(|v| v.to_string())
                                    .unwrap_or_else(|| "-".to_owned())
                            ),
                        )
                        .await;
                    }
                    Ok(WsMessage::Trade(trade)) => {
                        saw_message = true;
                        if attempt != 1 {
                            attempt = 1;
                        }
                        push_event(
                            &state,
                            format!(
                                "TRADE {} side={:?} px={} sz={}",
                                trade.id, trade.side, trade.price, trade.size
                            ),
                        )
                        .await;
                    }
                    Ok(_) => {
                        saw_message = true;
                        if attempt != 1 {
                            attempt = 1;
                        }
                    }
                    Err(err) => {
                        push_event(&state, format!("USER stream error: {}", err)).await;
                        break;
                    }
                }
            }

            let delay = reconnect_delay(attempt);
            let reason = if saw_message { "reconnect" } else { "retry" };
            push_event(
                &state,
                format!("USER stream {} in {}s", reason, delay.as_secs()),
            )
            .await;
            tokio::time::sleep(delay).await;
        }
    });
}

fn reconnect_delay(attempt: u32) -> std::time::Duration {
    let secs = match attempt {
        0 | 1 => 1,
        2 => 2,
        3 => 3,
        4 => 5,
        _ => 8,
    };
    std::time::Duration::from_secs(secs)
}

async fn push_event(state: &SharedState, line: String) {
    let mut events = state.recent_events.lock().await;
    events.push(line);
    if events.len() > 200 {
        let drain = events.len() - 200;
        events.drain(0..drain);
    }
}

fn parse_side(raw: &str) -> anyhow::Result<Side> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "buy" | "up" => Ok(Side::Buy),
        "sell" | "down" => Ok(Side::Sell),
        other => Err(anyhow::anyhow!("unsupported side '{other}'")),
    }
}

fn parse_order_type(raw: Option<&str>) -> OrderType {
    match raw.unwrap_or("FAK").trim().to_ascii_uppercase().as_str() {
        "FOK" => OrderType::FOK,
        "GTC" => OrderType::GTC,
        "GTD" => OrderType::GTD,
        _ => OrderType::FAK,
    }
}
