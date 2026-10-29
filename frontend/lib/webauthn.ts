// Browser-side WebAuthn / passkey ceremony glue. Converts the server's base64url option JSON
// (app/routers/webauthn.py, built by py_webauthn's options_to_json) into the ArrayBuffer shapes
// navigator.credentials.create/get require, runs the platform authenticator (Touch ID / Android
// fingerprint), and converts the credential back to the base64url JSON the server verifies.
//
// No secret ever lives here: the private key stays in the platform authenticator; the session is
// issued by the backend as HttpOnly cookies exactly like password login (see lib/api.ts).
import { api, CurrentUser } from "@/lib/api";

export class PasskeyUnsupportedError extends Error {
  constructor() {
    super("Den här enheten eller webbläsaren stödjer inte passkeys.");
    this.name = "PasskeyUnsupportedError";
  }
}

export function passkeysSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.PublicKeyCredential !== "undefined" &&
    !!navigator.credentials
  );
}

function b64urlToBuffer(value: string): ArrayBuffer {
  const pad = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + pad).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

function bufferToB64url(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/* eslint-disable @typescript-eslint/no-explicit-any */
function toCreationOptions(options: any): PublicKeyCredentialCreationOptions {
  return {
    ...options,
    challenge: b64urlToBuffer(options.challenge),
    user: { ...options.user, id: b64urlToBuffer(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((c: any) => ({
      ...c,
      id: b64urlToBuffer(c.id),
    })),
  };
}

function toRequestOptions(options: any): PublicKeyCredentialRequestOptions {
  return {
    ...options,
    challenge: b64urlToBuffer(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((c: any) => ({
      ...c,
      id: b64urlToBuffer(c.id),
    })),
  };
}

function attestationToJSON(cred: PublicKeyCredential): any {
  const r = cred.response as AuthenticatorAttestationResponse;
  return {
    id: cred.id,
    rawId: bufferToB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bufferToB64url(r.clientDataJSON),
      attestationObject: bufferToB64url(r.attestationObject),
      transports: typeof r.getTransports === "function" ? r.getTransports() : [],
    },
    clientExtensionResults: cred.getClientExtensionResults(),
  };
}

function assertionToJSON(cred: PublicKeyCredential): any {
  const r = cred.response as AuthenticatorAssertionResponse;
  return {
    id: cred.id,
    rawId: bufferToB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bufferToB64url(r.clientDataJSON),
      authenticatorData: bufferToB64url(r.authenticatorData),
      signature: bufferToB64url(r.signature),
      userHandle: r.userHandle ? bufferToB64url(r.userHandle) : null,
    },
    clientExtensionResults: cred.getClientExtensionResults(),
  };
}
/* eslint-enable @typescript-eslint/no-explicit-any */

/** Enroll a new passkey for the logged-in user. Runs the platform authenticator. */
export async function enrollPasskey(label: string): Promise<void> {
  if (!passkeysSupported()) throw new PasskeyUnsupportedError();
  const { handle, options } = await api.webauthnRegisterBegin();
  const cred = (await navigator.credentials.create({
    publicKey: toCreationOptions(options),
  })) as PublicKeyCredential | null;
  if (!cred) throw new Error("Registreringen avbröts.");
  await api.webauthnRegisterComplete(handle, attestationToJSON(cred), label);
}

/** Sign in with a passkey for the given account email. Returns the authenticated user. */
export async function loginWithPasskey(email: string): Promise<CurrentUser> {
  if (!passkeysSupported()) throw new PasskeyUnsupportedError();
  const { handle, options } = await api.webauthnAuthenticateBegin(email);
  const cred = (await navigator.credentials.get({
    publicKey: toRequestOptions(options),
  })) as PublicKeyCredential | null;
  if (!cred) throw new Error("Inloggningen avbröts.");
  return api.webauthnAuthenticateComplete(handle, assertionToJSON(cred));
}
