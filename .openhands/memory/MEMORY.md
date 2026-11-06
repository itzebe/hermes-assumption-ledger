# intent-pay — project memory

Next.js 15 + TypeScript intent-based payment app on **Monad Mainnet (chainId 143)**.
Repo `github.com/itzebe/intent-pay`; production `https://intent-pay-roan.vercel.app`
(Vercel project `intent-pay`, team `marvinsjones06-1839`).

## Commands (CI parity — `.github/workflows/ci.yml`)
- App: `npx tsc --noEmit` · `npm test` (vitest) · `npm run build`
- SDK (`packages/sdk`): `npm run typecheck` · `npm test` · `npm run build` (tsup)
- `npx next lint` is **not** configured (no eslint dep) and hangs on an install
  prompt — do not run it. CI does not lint.

## Environment quirks
- Container Node is 24.21.0; `engines.node` requires `>=24.22.1 <25` (Brotli RCE
  floor). CI uses node 24, so it passes; local `npm ci` warns but works.
- Playwright probes: use `playwright-core` from the npx cache with
  `executablePath: '/usr/bin/chromium'`, `waitUntil: 'domcontentloaded'`
  (`networkidle` times out on this app).
- Vercel CLI needs `--token "$VERCEL_TOKEN" --scope marvinsjones06-1839`; the
  project is not linked locally. Git metadata for a deployment is on the
  **v13 REST API** (`/v13/deployments/<id>` → `meta.githubCommitSha`), not the
  CLI `inspect --json`.

## Wallet discovery invariants (don't regress)
- **Trust Wallet's in-app browser is legacy-only** (no EIP-6963). It sets
  `window.ethereum` and `window.trustwallet.ethereum` *after* first paint, then
  dispatches `ethereum#initialized`. Discovery must read both globals, listen for
  that event, **and** keep polling — a single mount-time read reports "no browser
  wallet detected" on mobile.
- **The poll must not stop at the fast window.** `watchWallets` polls 250 ms for
  6 s (`LEGACY_POLL_WINDOW_MS`), then **drops to a 2 s slow poll
  (`LEGACY_SLOW_POLL_MS`) until `destroy`**. Measured: a Trust-style provider
  injected at ~9 s (past the old 6 s stop) was never detected on production until
  the manual Rescan — that was the residual Defect A (PR #38, main `69ea6d6`).
  `WalletPicker` (`components/wallet/WalletBar.tsx`) also re-scans on open. Keep
  `lib/wallet/eip6963.ts` and `packages/sdk/src/wallet/eip6963.ts` in step.
- Legacy provider ids must be derived from **identity flags**, never the index in
  `window.ethereum.providers` (order is not stable across loads). Keep the id and
  name priority orders identical (`legacyWalletId` / `legacyProviderName`) or a
  provider's id and label can disagree.
- Silent reconnect must only retry when a stored injected connector was *absent*
  on the first attempt. An unscoped retry resurrects a deliberately disconnected
  wallet (`tests/walletIntegration.test.tsx` guards this).
- The app (`lib/wallet/eip6963.ts`) and SDK (`packages/sdk/src/wallet/eip6963.ts`)
  are two copies of the same logic — change both.

## Routing / RPC reliability (don't regress)
- Every chain read goes through a **fallback transport** over
  `serverRpcUrls()` / `browserRpcUrls()` (`lib/config/chains.ts`), which always
  end at the key-free public `rpc.monad.xyz`. A single-endpoint keyed Alchemy URL
  (HTTP 429, empty body) once failed every pool read and receipt wait.
- An **unreachable RPC is transient `provider_error`, never `route_unavailable`**
  (a definitive code that clears the held quote and stops retries). See
  `isRpcUnreachable` + `routeFailure` in `lib/providers/uniswapV3.ts`. A
  QuoterV2 revert **carrying encoded data** is still a real on-chain answer.
- A graph built during an outage must **not** be cached for `GRAPH_TTL_MS`.
- `shouldRetryQuote` (`lib/domain/quoteState.ts`) is the single retry rule and
  covers a transient failure with **no held quote** (a cold quote during a blip),
  not only a refresh failure with a held quote.

## Checkout invariants
- The server payload is untrusted: a missing/mistyped `receiveToken`, `recipient`
  or `amount` used to throw in render (`receiveToken.slice`) and blank the page.
  `parseCheckoutView` in `components/checkout/CheckoutClient.tsx` is the gate;
  never read a server field directly in the render path.
- Non-vacuous regression tests matter: verify a new test fails when its fix is
  reverted (did this for both Defects A and B).

## Process notes
- PRs from this environment are created **as drafts**; `merge-async` refuses a
  draft. `gh pr ready <n>` first.
- Merge with `gh api -X PUT repos/itzebe/intent-pay/pulls/<n>/merge-async
  -f merge_method=squash -f sha=<head>` then poll
  `.../merge-async/<uuid>` until `status` is not `pending`.
- The repo squashes (PR #35 landed as a single-parent commit).
- The stored dev key in `/tmp/devkey.txt` is **not** valid on production (separate
  store) — create a fresh key via the dev console for prod API probes.

## Wallet CSP + Trust Wallet RPC (Mission 18, 2026-11-06, `572743d`)
- **A page-world injected wallet's chain read is subject to OUR CSP.** Trust
  Wallet's Android in-app browser answers `eth_chainId`/`eth_call` by proxying to
  its own node **from the page context**. `next.config.mjs` `connect-src` must
  include `injectedWalletRpcOrigins` (`https://monad.twnodes.com`) or `connect()`
  throws a bare `TypeError: Failed to fetch` (no EIP-1193 code) → "Couldn't
  connect to the wallet". The wallet is still *discovered/listed*; only the read
  fails. Never remove that origin; it is what makes direct injected connect work
  without WalletConnect.
- **`monad.twnodes.com` is Trust Wallet's own RPC** (`x-tw-naas-request-id`,
  Alchemy-backed), in no repo file / no Vercel env. `http://monad.twnodes.com/`
  `301 → https://monad.twnodes.com:443/` (explains the `http://`+`:443` error).
  We hand wallets `https://rpc.monad.xyz`. Don't "fix" it by editing our config.
- `useWallet.connect()` uses `safeChainId()`: a failed chain read must NOT fail
  the connection (chain reported unknown, never coerced to Monad). Empty account
  list = honest message, never a phantom connect; failed injected attempt never
  falls back to WalletConnect.
- PRs from this env open **as drafts** → `gh pr ready` before `merge-async`.

## Intent Pay — picker de-dupe + receipt failover (Mission 19, 2026-11-06, PR #42 `38636c1`)
- **A wallet must be listed once.** `watchWallets` reads the legacy slot *and*
  EIP-6963 announcements; a pre-boot wallet is added from the legacy slot then
  announces the **same provider object** with a different id (`legacy:*`). De-dupe
  in **both** `onAnnounce` and `addLegacy` **by provider identity**, not by
  uuid/id alone — app `lib/wallet/eip6963.ts` and SDK `packages/sdk/src/wallet/eip6963.ts`.
  Two "MetaMask" rows = the legacy row (no announcement) was picked → "Connect
  Wallet does not reliably connect to the injected wallet".
- **A receipt read must fail over on `null`, not only on a throw.** viem `fallback`
  treats a `null` receipt as success and stops, so `waitForTransactionReceipt` can
  poll one lagging node for its full 90 s. `lib/rpc/receiptFailover.ts`
  (`receiptFailoverTransport`) tries every endpoint and takes the first non-null;
  used by `lib/wallet/clients.ts` (browser) and `lib/server/rpc.ts` (server). This
  is the "hangs on Wrap MON / confirming on-chain" defect. Do not wrap a receipt
  path in a bare `fallback()` again. Regression `tests/receiptFailover.test.ts`
  (real viem clients over real `custom` transports; raw hex receipt fixture).
- Prod alias `intent-pay-roan.vercel.app` = deployment `dpl_DrT8Z8o4oCFGLuXbQaxo2mtjxBmo`
  (both fixes live; verified by `scripts/mission19/dupProbe.mjs` → `rows: 1`).

## App-owned RPC vs the wallet's own RPC (Mission 21, 2026-11-06, PR #43 `a066c6e`)
- **Intent Pay performs every chain read through its own HTTPS clients**; the
  wallet is asked only to authorise/broadcast. Measured (`scripts/mission20/
  viemMethods.mjs`): viem sends **only** `eth_chainId` + `eth_sendTransaction`
  through the wallet — never gas/nonce/balance/receipt. Pinned in
  `tests/appOwnedRpc.test.ts`. Trust's provider answers `eth_chainId` locally, so
  the `http://monad.twnodes.com` fetch is the *wallet's own* reads.
- **`http://monad.twnodes.com/...:443` is NOT our RPC failing.** `classifyTransactionError(err,
  {source:"wallet"})` → `kind:"wallet_rpc"` (`mayHaveSubmitted:false`, "the
  transaction was not sent. Intent Pay's own chain reads are unaffected…"), never
  `rpc_unavailable`. `isWalletTransportFailure` keys off a **stepId** (executePlan
  tags a submission failure; our reads never carry one). `classifyWalletError`
  has `rpc_transport`. `sendCalls.ts` failure carries the thrown `cause`.
- **HTTPS-only endpoints:** `lib/rpc/endpoints.ts` (`httpsEndpoints`) replaces the
  ad-hoc `dedupe` in `lib/config/chains.ts`; a plaintext `http://` `*_RPC_URL` is
  dropped, never downgraded.
- Read-only forensic probes live in `scripts/mission20/` (README explains each).


## Hermes Plugin Hackathon — outcome-verification verdict (2026-11-06)
- **"Receipt for an agent's completion claim" is a saturated genre.** Merged into the catalog before
  the mission: `say-do-gate` (claims vs tool log), `lineage` (provenance receipts), `verifyum`
  (Solana proofs), `invinoveritas-receipts` (signed approval receipts), `jackal-verified`. Not yet
  merged but live: `proofops` (PR #149625, `mergeable_state=blocked`), `prove-it`, `no-cap`,
  `completion-proof`, `hermes-ref`. Judging is on **usefulness, reliability, creativity** → a
  derivative loses on the last one.
- The one genuinely open slice is **external deployment identity** (assert the *deployed* commit /
  Vercel deployment uid / production alias / live health) — **0 of 1,063 catalog entries reference
  `api.vercel.com`**, only 9 reference `api.github.com`, and none asserts a deployed commit. But it is
  a *narrow* differentiator in an already-crowded genre, needs Vercel+GitHub tokens (network
  dependency = the "API 200 ≠ outcome" trap), so it is a risky first-place bet.
- Feasibility drag: **~1,826 open plugin-catalog PRs, ~60 new/day, 1,354 merged** → maintainer review
  latency is the bottleneck, not code. Landing a catalog entry inside 5 days is not guaranteed.
- Vercel `/v6/deployments` returns `meta.githubCommitSha`, `state`, `target`, `url`; `/v9/projects/<p>/domains`
  returns the production alias + `verified` (verified live on the user's own `intent-pay` project).
  `/v4/aliases?projectId=` 404s for a project-scoped query — use the project domains endpoint.
- Catalog gaps worth targeting instead: `blast radius` 0 entries, `assumption` 0, `acceptance criteria`
  0, `flaky` 0; under-used hooks `agent_loop_stopped` (4), `post_compaction` (3).
