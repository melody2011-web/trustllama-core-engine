import "./solana-buffer";
import { useState, useEffect, useRef } from "react";
import { PublicKey } from "@solana/web3.js";
import { encodeURL } from "@solana/pay";
import BigNumber from "bignumber.js";
import { QRCodeSVG } from "qrcode.react";
import { 
  ArrowLeft, Copy, CheckCircle2, AlertTriangle, Download, 
  RefreshCcw, Clock, Lock, Loader2
} from "lucide-react";

interface IntentResponse {
  id: string;
  checkoutToken: string;
  tier: string;
  tierName: string;
  amountUsdc: string;
  recipient: string;
  splToken: string;
  reference: string;
  label: string;
  message: string;
  expiresAt: string;
}

interface CheckoutFlowProps {
  tier: "indie" | "startup" | "enterprise" | "webhook_rail_pro";
  onCancel: () => void;
}

export function CheckoutFlow({ tier, onCancel }: CheckoutFlowProps) {
  const [intent, setIntent] = useState<IntentResponse | null>(null);
  const [status, setStatus] = useState<"initializing" | "pending" | "verified" | "expired" | "error">("initializing");
  const [errorMsg, setErrorMsg] = useState("");
  const [downloadInfo, setDownloadInfo] = useState<{ url: string, expiresAt: string } | null>(null);
  const [verifiedSignature, setVerifiedSignature] = useState<string | null>(null);
  const [deliveryMessage, setDeliveryMessage] = useState("");
  const [copiedField, setCopiedField] = useState<string | null>(null);
  const [pollWarning, setPollWarning] = useState("");
  
  const pollingRef = useRef<NodeJS.Timeout | null>(null);
  const mountedRef = useRef(true);
  const autoDownloadRef = useRef<string | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    createIntent();
    
    return () => {
      mountedRef.current = false;
      stopPolling();
    };
  }, [tier]);

  useEffect(() => {
    if (tier !== "webhook_rail_pro" || status !== "verified" || !downloadInfo) return;
    if (autoDownloadRef.current === downloadInfo.url) return;
    autoDownloadRef.current = downloadInfo.url;
    const link = document.createElement("a");
    link.href = downloadInfo.url;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }, [tier, status, downloadInfo]);

  const createIntent = async () => {
    try {
      setStatus("initializing");
      setPollWarning("");
      setDownloadInfo(null);
      autoDownloadRef.current = null;
      setVerifiedSignature(null);
      setDeliveryMessage("");
      const res = await fetch("/api/licensing/intents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tier })
      });
      
      if (!res.ok) {
        const failure = await res.json().catch(() => null);
        throw new Error(typeof failure?.error === "string" ? failure.error : `Server returned status ${res.status}`);
      }
      
      const data = await res.json();
      if (!data.ok || !data.intent) {
        throw new Error("Invalid response from server");
      }
      
      if (mountedRef.current) {
        setIntent(data.intent);
        setStatus("pending");
        startPolling(data.intent.id, data.intent.checkoutToken);
      }
    } catch (err: any) {
      if (mountedRef.current) {
        setStatus("error");
        setErrorMsg(err.message || "Failed to initialize checkout");
      }
    }
  };

  const startPolling = (intentId: string, checkoutToken: string) => {
    stopPolling();
    pollingRef.current = setInterval(async () => {
      try {
        if (!checkoutToken) {
          setPollWarning("Checkout authorization is missing. Generate a new checkout.");
          stopPolling();
          return;
        }
        const res = await fetch(`/api/licensing/intents/${intentId}/status`, {
          headers: { "X-Tlama-License-Checkout": checkoutToken },
        });
        if (!res.ok) {
          setPollWarning("Settlement verification is retrying after a temporary service error.");
          return; 
        }
        
        const data = await res.json();
        if (!data.ok) return;
        
        setPollWarning("");
        if (data.status === "verified" && mountedRef.current) {
          if (typeof data.signature === "string") setVerifiedSignature(data.signature);
          if (typeof data.downloadUrl === "string" && typeof data.downloadExpiresAt === "string") {
            setDownloadInfo({
              url: data.downloadUrl,
              expiresAt: data.downloadExpiresAt
            });
            setStatus("verified");
            stopPolling();
          } else {
            setDeliveryMessage(typeof data.message === "string" ? data.message : "Payment verified, but the download is not available. Please contact support.");
            setStatus("verified");
            stopPolling();
          }
        } else if (data.status === "expired" && mountedRef.current) {
          setStatus("expired");
          stopPolling();
        }
      } catch (err) {
        console.error("Network error during polling", err);
        if (mountedRef.current) {
          setPollWarning("Network verification is temporarily unavailable. Retrying automatically.");
        }
      }
    }, 5000);
  };

  const stopPolling = () => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  };

  const handleCopy = (text: string, field: string) => {
    navigator.clipboard.writeText(text);
    setCopiedField(field);
    setTimeout(() => {
      if (mountedRef.current) setCopiedField(null);
    }, 2000);
  };

  if (status === "initializing") {
    return (
      <div className="w-full flex-1 flex flex-col items-center justify-center animate-fade-in py-32 text-center">
        <div className="w-16 h-16 rounded-2xl bg-primary/10 text-primary flex items-center justify-center mb-6">
          <Loader2 className="w-8 h-8 animate-spin" />
        </div>
        <h2 className="text-2xl font-display font-bold mb-2">Generating Secure Contract...</h2>
        <p className="text-muted-foreground">Provisioning your dedicated settlement reference</p>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="w-full max-w-2xl mx-auto animate-fade-in py-16">
        <div className="bg-destructive/5 border border-destructive/20 rounded-3xl p-10 text-center">
          <div className="w-16 h-16 rounded-2xl bg-destructive/10 text-destructive flex items-center justify-center mx-auto mb-6">
            <AlertTriangle className="w-8 h-8" />
          </div>
          <h2 className="text-2xl font-display font-bold text-destructive mb-3">Checkout Initialization Failed</h2>
          <p className="text-destructive/80 mb-10 max-w-md mx-auto">{errorMsg}</p>
          <div className="flex flex-col sm:flex-row items-center justify-center gap-4">
            <button 
              onClick={onCancel}
              className="w-full sm:w-auto px-8 py-4 rounded-xl border-2 border-destructive/20 text-destructive hover:bg-destructive/10 font-bold transition-all"
            >
              Go Back
            </button>
            <button 
              onClick={createIntent}
              className="w-full sm:w-auto px-8 py-4 rounded-xl bg-destructive text-destructive-foreground hover:bg-destructive/90 font-bold transition-all flex items-center justify-center gap-2 shadow-lg shadow-destructive/20"
            >
              <RefreshCcw className="w-5 h-5" /> Try Again
            </button>
          </div>
        </div>
      </div>
    );
  }

  let solanaUrl = "";
  if (intent) {
    try {
      const url = encodeURL({
        recipient: new PublicKey(intent.recipient),
        amount: new BigNumber(intent.amountUsdc),
        splToken: new PublicKey(intent.splToken),
        reference: new PublicKey(intent.reference),
        label: intent.label,
        message: intent.message
      });
      solanaUrl = url.toString();
    } catch (err) {
      console.error("Failed to encode Solana Pay URL", err);
    }
  }

  return (
    <div className="w-full mx-auto animate-fade-in">
      <button 
        onClick={onCancel}
        className="group flex items-center gap-2 text-sm font-bold text-muted-foreground hover:text-foreground transition-colors mb-10"
      >
        <ArrowLeft className="w-4 h-4 transition-transform group-hover:-translate-x-1" /> {tier === "webhook_rail_pro" ? "Back to products" : "Change Tier"}
      </button>

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
        {/* Left Column - Details */}
        <div className="lg:col-span-7 flex flex-col gap-6">
          <div className="bg-card border rounded-[2rem] p-8 sm:p-10 shadow-sm">
            <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-4 mb-10">
              <div>
                <h1 className="text-3xl font-display font-bold tracking-tight mb-2">Complete Purchase</h1>
                <p className="text-muted-foreground text-lg">Scan with a Solana Pay compatible wallet to preserve the required settlement reference.</p>
              </div>
              <div className="inline-flex px-4 py-2 bg-primary/10 text-primary rounded-xl text-xs font-bold uppercase tracking-widest border border-primary/20 shrink-0">
                {intent?.tierName || tier}
              </div>
            </div>

            <div className="space-y-8">
              <div className="flex flex-col sm:flex-row sm:items-end justify-between py-6 border-y border-border/50 gap-4">
                <span className="text-muted-foreground font-semibold uppercase tracking-wider text-sm">Total Amount</span>
                <div className="flex items-baseline gap-2">
                  <span className="text-5xl font-display font-bold tracking-tighter">{intent?.amountUsdc}</span>
                  <span className="text-xl font-bold text-primary">USDC</span>
                </div>
              </div>

              {/* Inspection Details */}
              <div className="space-y-5">
                <div>
                  <label className="text-xs font-bold text-muted-foreground uppercase tracking-wider mb-2 block">
                    Recipient Address
                  </label>
                  <div className="flex items-center gap-3">
                    <code className="flex-1 bg-muted/50 px-4 py-3.5 rounded-xl text-sm font-mono text-foreground truncate border border-border/50">
                      {intent?.recipient}
                    </code>
                    <button 
                      onClick={() => intent && handleCopy(intent.recipient, "recipient")}
                      className="p-3.5 rounded-xl border border-border/50 bg-muted/50 hover:bg-muted text-muted-foreground hover:text-foreground transition-all hover:scale-105 active:scale-95 shrink-0"
                      title="Copy Address"
                      aria-label="Copy recipient address"
                    >
                      {copiedField === "recipient" ? <CheckCircle2 className="w-5 h-5 text-green-500" /> : <Copy className="w-5 h-5" />}
                    </button>
                  </div>
                </div>

                <div>
                  <label className="text-xs font-bold text-muted-foreground uppercase tracking-wider mb-2 block">
                    USDC Mint (Solana Mainnet)
                  </label>
                  <div className="flex items-center gap-3">
                    <code className="flex-1 bg-muted/50 px-4 py-3.5 rounded-xl text-sm font-mono text-foreground truncate border border-border/50">
                      {intent?.splToken}
                    </code>
                    <button 
                      onClick={() => intent && handleCopy(intent.splToken, "mint")}
                      className="p-3.5 rounded-xl border border-border/50 bg-muted/50 hover:bg-muted text-muted-foreground hover:text-foreground transition-all hover:scale-105 active:scale-95 shrink-0"
                      title="Copy Mint Address"
                      aria-label="Copy mint address"
                    >
                      {copiedField === "mint" ? <CheckCircle2 className="w-5 h-5 text-green-500" /> : <Copy className="w-5 h-5" />}
                    </button>
                  </div>
                </div>
                
                <div>
                  <label className="text-xs font-bold text-primary uppercase tracking-wider mb-2 flex items-center gap-2">
                    <Lock className="w-3.5 h-3.5" /> Settlement Reference (Do Not Modify)
                  </label>
                  <div className="flex items-center gap-3">
                    <code className="flex-1 bg-primary/5 px-4 py-3.5 rounded-xl text-sm font-mono text-primary truncate border border-primary/20">
                      {intent?.reference}
                    </code>
                    <button 
                      onClick={() => intent && handleCopy(intent.reference, "ref")}
                      className="p-3.5 rounded-xl border border-primary/20 bg-primary/5 hover:bg-primary/10 text-primary transition-all hover:scale-105 active:scale-95 shrink-0"
                      title="Copy Reference"
                      aria-label="Copy reference"
                    >
                      {copiedField === "ref" ? <CheckCircle2 className="w-5 h-5" /> : <Copy className="w-5 h-5" />}
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <div className="mt-10 bg-amber-500/10 border border-amber-500/20 rounded-2xl p-5 flex items-start gap-4">
              <div className="bg-amber-500/20 p-2 rounded-xl shrink-0">
                <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400" />
              </div>
              <div className="text-sm text-amber-700 dark:text-amber-400">
                <p className="font-bold mb-1 text-base">Send ONLY native USDC on Solana Mainnet.</p>
                <p className="leading-relaxed opacity-90">Sending SOL, USDT, bridged USDC, or using another network may result in permanent loss of funds. Complete payment through this Solana Pay request so the unique reference is preserved.</p>
              </div>
            </div>
          </div>
        </div>

        {/* Right Column - Status & QR */}
        <div className="lg:col-span-5 flex flex-col gap-6 sticky top-28">
          <div className="bg-card border rounded-[2rem] overflow-hidden flex flex-col shadow-sm">
            
            {/* Dynamic Header based on status */}
            <div className={`p-8 border-b flex flex-col items-center justify-center text-center transition-colors duration-500 ${
              status === "verified" ? "bg-green-500/10 border-green-500/20" :
              status === "expired" ? "bg-destructive/10 border-destructive/20" :
              "bg-primary/5 border-border/50"
            }`}>
              {status === "pending" && (
                <>
                  <div className="relative mb-6">
                    <div className="absolute inset-0 border-4 border-primary/20 rounded-full"></div>
                    <div className="absolute inset-0 border-4 border-primary border-t-transparent rounded-full animate-spin"></div>
                    <div className="w-16 h-16 flex items-center justify-center text-primary">
                      <Lock className="w-6 h-6" />
                    </div>
                  </div>
                  <h3 className="text-xl font-display font-bold text-primary max-w-[250px] leading-tight mx-auto">Awaiting USDC Network Settlement Block...</h3>
                  <div className="mt-4 inline-flex items-center gap-1.5 px-3 py-1 bg-background/50 rounded-full border border-border/50 text-xs font-medium text-muted-foreground shadow-sm">
                    <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse"></span>
                    Auto-verifying in background
                  </div>
                   {pollWarning ? (
                     <p className="mt-3 max-w-xs text-xs font-medium text-amber-700 dark:text-amber-400">
                       {pollWarning}
                     </p>
                   ) : null}
                </>
              )}
              
              {status === "verified" && (
                <>
                  <div className="w-20 h-20 rounded-full bg-green-500/20 text-green-600 dark:text-green-400 flex items-center justify-center mb-5 ring-4 ring-green-500/10 shadow-lg shadow-green-500/10">
                    <CheckCircle2 className="w-10 h-10" />
                  </div>
                  <h3 className="text-2xl font-display font-bold text-green-600 dark:text-green-400">Payment Verified successfully</h3>
                  <p className="text-sm font-medium text-green-700/80 dark:text-green-400/80 mt-2">Cryptographic settlement confirmed.</p>
                </>
              )}

              {status === "expired" && (
                <>
                  <div className="w-20 h-20 rounded-full bg-destructive/20 text-destructive flex items-center justify-center mb-5 ring-4 ring-destructive/10">
                    <Clock className="w-10 h-10" />
                  </div>
                  <h3 className="text-2xl font-display font-bold text-destructive">Checkout Expired</h3>
                  <p className="text-sm font-medium text-destructive/80 mt-2">The settlement window has closed.</p>
                </>
              )}
            </div>

            <div className="p-8 sm:p-10 flex flex-col items-center justify-center bg-background/30 flex-1">
              {status === "pending" && solanaUrl && (
                <div className="w-full flex flex-col items-center">
                  <div className="bg-white p-6 rounded-3xl shadow-sm border border-border/50 mb-8 w-full max-w-[280px] aspect-square flex items-center justify-center">
                    <QRCodeSVG 
                      value={solanaUrl}
                      size={240}
                      level="H"
                      includeMargin={false}
                      className="w-full h-auto"
                    />
                  </div>
                  
                  <a 
                    href={solanaUrl}
                    className="w-full py-4 px-6 border-2 border-primary/20 text-primary hover:bg-primary/5 rounded-xl font-bold flex items-center justify-center gap-2 transition-colors mb-6 md:hidden"
                  >
                    Open Wallet App
                  </a>
                  
                  <div className="w-full bg-card border rounded-xl p-4 text-center">
                    <p className="text-sm font-bold text-foreground mb-1">
                      Expires at {intent ? new Date(intent.expiresAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '...'}
                    </p>
                    <p className="text-xs text-muted-foreground">Do not close this page during settlement.</p>
                  </div>
                </div>
              )}

              {status === "verified" && downloadInfo && (
                <div className="w-full text-center">
                  {tier === "webhook_rail_pro" && (
                    <p className="mb-4 text-sm text-muted-foreground">
                      Your package download starts automatically. If your browser blocks it, use the button below.
                    </p>
                  )}
                  <a 
                    href={downloadInfo.url}
                    target="_blank"
                    rel="noreferrer"
                    className="w-full py-5 px-6 bg-green-600 text-white rounded-2xl font-bold text-lg flex items-center justify-center gap-3 hover:bg-green-700 hover:-translate-y-1 transition-all shadow-xl shadow-green-600/20 mb-6"
                  >
                    <Download className="w-6 h-6" />
                    {tier === "webhook_rail_pro" ? "Download Webhook Rail Pro" : "Download Release"}
                  </a>
                  <div className="inline-flex items-center gap-2 text-xs font-medium text-muted-foreground bg-muted/50 px-4 py-2 rounded-full border">
                    <Lock className="w-3.5 h-3.5" /> 
                    Link expires {new Date(downloadInfo.expiresAt).toLocaleTimeString()}
                  </div>
                </div>
              )}
              {status === "verified" && !downloadInfo && (
                <div className="w-full text-center">
                  <p className="font-medium text-foreground">{deliveryMessage}</p>
                  {tier === "webhook_rail_pro" && (
                    <p className="mt-3 text-sm text-muted-foreground">
                      Save your settlement signature as proof of purchase. For fulfillment help, contact{" "}
                      <a href="https://t.me/trustllama" target="_blank" rel="noreferrer" className="text-primary underline">TrustLlama on Telegram</a>.
                      Never send your wallet seed phrase or private keys.
                    </p>
                  )}
                  {verifiedSignature && <code className="mt-4 block break-all rounded-xl border border-border bg-muted/50 p-3 text-xs text-foreground">{verifiedSignature}</code>}
                </div>
              )}

              {status === "expired" && (
                <button
                  onClick={createIntent}
                  className="w-full py-4 px-6 bg-foreground text-background rounded-xl font-bold flex items-center justify-center gap-3 hover:bg-foreground/90 transition-all shadow-lg hover:-translate-y-0.5"
                >
                  <RefreshCcw className="w-5 h-5" />
                  Generate New Contract
                </button>
              )}
            </div>
            
          </div>
        </div>
      </div>
    </div>
  );
}
