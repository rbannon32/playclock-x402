/**
 * Bundle entry point — the only thing esbuild compiles.
 *
 * `npm run build` turns this into `web/dist/wallet.js`, a single ES module
 * carrying algosdk and both wallet SDKs (~1.2MB). Everything else in `web/js/`
 * stays plain source served as-is, and `payment.js` imports this file
 * **dynamically**, only once someone actually reaches for a wallet. So the
 * docs pages, the free preview and the whole mock-mode demo still load without
 * the bundle existing at all — which also means `npm run build` is not needed
 * for local development.
 */

export {
  ALGOD_URLS,
  EXPLORERS,
  UnsupportedQuote,
  algodFor,
  buildExactAvmPayload,
  buildPaymentGroup,
  explorerUrl,
  readQuote,
} from "./exact-avm.js";

export { WALLET_IDS, WalletRejected, alignSignatures, connectWallet, isRejection } from "./connectors.js";
