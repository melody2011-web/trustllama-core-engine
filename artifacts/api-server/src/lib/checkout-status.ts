type VerifiedCheckout = {
  id: string;
  tier: string;
  signature: string | null;
  download_expires_at: Date | null;
  downloaded_at: Date | null;
};

export function verifiedCheckoutStatus(
  intent: VerifiedCheckout,
  downloadToken: (intent: VerifiedCheckout) => string,
) {
  if (
    !intent.downloaded_at &&
    intent.download_expires_at &&
    intent.download_expires_at > new Date()
  ) {
    return {
      ok: true,
      status: "verified",
      signature: intent.signature,
      downloadUrl: `/api/licensing/download/${intent.id}/${downloadToken(intent)}`,
      downloadExpiresAt: intent.download_expires_at.toISOString(),
    };
  }

  return {
    ok: true,
    status: "verified",
    signature: intent.signature,
    downloadUrl: null,
    message: "Payment is verified, but the download grant is no longer available. Contact support if you could not retrieve your package.",
  };
}