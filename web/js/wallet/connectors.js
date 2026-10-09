/**
 * Pera and Defly, behind one small interface.
 *
 * Both expose almost the same surface (`connect`, `reconnectSession`,
 * `disconnect`, `signTransaction`), so the wrapper is thin. What it is really
 * for is normalising two things the
 * SDKs disagree about, each of which is a silent wrong-money bug if guessed:
 *
 *  - **which network** — they take a numeric `chainId`, not the CAIP-2 id the
 *    402 quotes, so a mis-mapping signs against the wrong ledger;
 *  - **what comes back from `signTransaction`** — see `alignSignatures`.
 */

import algosdk from "algosdk";
import { PeraWalletConnect } from "@perawallet/connect";
import { DeflyWalletConnect } from "@blockshake/defly-connect";

/** CAIP-2 network id -> the numeric chain id the wallet SDKs expect. */
const CHAIN_IDS = Object.freeze({
  "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=": 416001, // MainNet
  "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=": 416002, // TestNet
});

/** Thrown when the person closes the wallet without approving. Not an error state. */
export class WalletRejected extends Error {
  constructor(message = "Wallet request was dismissed.") {
    super(message);
    this.name = "WalletRejected";
    this.cancelled = true;
  }
}

/**
 * Whether a thrown wallet error means "the human said no".
 *
 * Neither SDK throws a typed error, so this reads the shapes they actually
 * produce. Getting it wrong turns a dismissed prompt into a scary red failure.
 */
export function isRejection(error) {
  if (!error) return false;
  if (error.cancelled || error instanceof WalletRejected) return true;
  const text = `${error.message || ""} ${error.data?.type || ""}`.toLowerCase();
  return (
    text.includes("cancell") ||
    text.includes("canceled") ||
    text.includes("reject") ||
    // Pera and Defly say "Connect modal is closed by user". A bare "closed"
    // would also swallow a real "Connection closed" and call it a dismissal.
    text.includes("closed by user") ||
    text.includes("modal closed") ||
    text.includes("dismiss")
  );
}

/**
 * Line up a wallet's return value with the group slots it was asked to sign.
 *
 * Pera and Defly have both shipped versions that return a **slot-aligned**
 * array (nulls where they did not sign) and versions that return only the
 * blobs they actually signed. Assuming either one is how a sponsored group
 * ends up with the signature in the wrong slot, so handle both by length.
 *
 * @param {(Uint8Array|null)[]} returned whatever the SDK gave back
 * @param {number} groupSize how many transactions were in the group
 * @param {number[]} indexesToSign the slots the wallet was asked to sign
 * @returns {(Uint8Array|null)[]} slot-aligned, length `groupSize`
 */
export function alignSignatures(returned, groupSize, indexesToSign) {
  const blobs = Array.isArray(returned) ? returned : [];
  if (blobs.length === groupSize) return blobs.map((b) => b || null);

  const aligned = new Array(groupSize).fill(null);
  indexesToSign.forEach((slot, i) => {
    aligned[slot] = blobs[i] || null;
  });
  return aligned;
}

/** One connected wallet: an address, a signer, and a way to let go. */
class Session {
  constructor(kind, label, sdk, address) {
    this.kind = kind;
    this.label = label;
    this.sdk = sdk;
    this.address = address;
  }

  /**
   * @param {Uint8Array[]} unsigned encoded unsigned transactions, one per slot
   * @param {number[]} indexesToSign slots whose sender is this wallet
   * @returns {Promise<(Uint8Array|null)[]>} slot-aligned signatures
   */
  async signTransactions(unsigned, indexesToSign) {
    const group = unsigned.map((bytes, index) => ({
      txn: algosdk.decodeUnsignedTransaction(bytes),
      signers: indexesToSign.includes(index) ? [this.address] : [],
    }));
    try {
      const returned = await this.sdk.signTransaction([group]);
      return alignSignatures(returned, unsigned.length, indexesToSign);
    } catch (error) {
      if (isRejection(error)) throw new WalletRejected();
      throw error;
    }
  }

  async disconnect() {
    try {
      await this.sdk.disconnect();
    } catch {
      // Disconnecting is best-effort: a wallet that has already gone away is
      // the outcome we wanted anyway.
    }
  }
}

const WALLETS = {
  pera: { label: "Pera", make: (chainId) => new PeraWalletConnect({ chainId }) },
  defly: { label: "Defly", make: (chainId) => new DeflyWalletConnect({ chainId }) },
};

/** Wallet ids this build supports, for rendering a chooser. */
export const WALLET_IDS = Object.keys(WALLETS);

/**
 * Connect a wallet, or silently restore a session from a previous page load.
 *
 * @param {object} args
 * @param {string} args.kind `"pera"` or `"defly"`
 * @param {string} args.network CAIP-2 network the 402 quoted
 * @param {boolean} [args.reconnectOnly] only restore; never open a modal
 * @returns {Promise<Session|null>} null when `reconnectOnly` found nothing
 */
export async function connectWallet({ kind, network, reconnectOnly = false }) {
  const wallet = WALLETS[kind];
  if (!wallet) throw new Error(`Unknown wallet ${kind}.`);
  const chainId = CHAIN_IDS[network];
  if (!chainId) {
    throw new Error(`This page cannot connect a wallet for ${network || "an unknown network"}.`);
  }

  const sdk = wallet.make(chainId);
  let accounts = [];
  try {
    // Always try to restore first: reconnecting a live session is instant and
    // silent, where connect() opens a modal the person has already dismissed
    // once by navigating.
    accounts = (await sdk.reconnectSession()) || [];
    if (!accounts.length && !reconnectOnly) accounts = (await sdk.connect()) || [];
  } catch (error) {
    if (isRejection(error)) throw new WalletRejected();
    throw error;
  }

  if (!accounts.length) return null;
  return new Session(kind, wallet.label, sdk, accounts[0]);
}
