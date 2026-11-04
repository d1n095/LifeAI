export type HealthEvidence = {
  status?: string;
  worker?: string;
  backend_reachable?: boolean;
  evidence_available?: boolean;
};

export type StopEvidence = {
  blocked?: boolean;
  code?: string | null;
  observability_failed?: boolean;
};

export type ReadinessEvidence = {
  level?: string;
  blocking?: string[];
  unknown_fail_closed?: boolean;
  evidence_unavailable?: boolean;
};

export function healthIsReachable(health: HealthEvidence | undefined): boolean {
  return health?.evidence_available !== false && health?.status === "ok" && health?.backend_reachable === true;
}

export function healthEvidenceMissing(health: HealthEvidence | undefined): boolean {
  if (!health) return true;
  if (health.evidence_available === false) return true;
  const status = health.status || "";
  return !status || status === "unknown";
}

export function describeHealth(health: HealthEvidence | undefined): string {
  if (healthIsReachable(health)) return "Backend nåbar";
  if (healthEvidenceMissing(health)) return "Backend-status okänd";
  return "Backend otillgänglig";
}

export function describeStop(stop: StopEvidence | undefined): string {
  if (!stop || stop.observability_failed !== false) {
    return "STOP-status okänd (fail-closed)";
  }
  return stop.blocked ? "STOP blockerar" : "STOP inaktiv";
}

export function describeReadiness(readiness: ReadinessEvidence | undefined): string {
  if (!readiness || readiness.evidence_unavailable || !readiness.level || readiness.level === "UNKNOWN") {
    return "beredskap okänd (fail-closed)";
  }
  if (readiness.level === "BLOCKED") {
    const n = (readiness.blocking || []).length;
    if (n === 0) return "beredskap okänd (fail-closed)";
    return `beredskap BLOCKED (${n})`;
  }
  return `beredskap ${readiness.level}`;
}

export function statusLine(
  snapshot: {
    health: HealthEvidence;
    startup_readiness: ReadinessEvidence;
    stop: StopEvidence;
    safe_launch: { privileged_remote_effects_enabled?: boolean; recall_http_mounted?: boolean };
    first_cycle_go?: boolean;
  } | null,
  error: string | null,
  loaded: boolean,
): string {
  if (!loaded) return "Hämtar systemstatus…";
  if (!snapshot) {
    if (error) return "Backend otillgänglig — ingen driftsstatus påstås.";
    return "Founder-status inte inläst.";
  }
  const parts = [
    describeHealth(snapshot.health),
    describeReadiness(snapshot.startup_readiness),
    describeStop(snapshot.stop),
    snapshot.safe_launch.privileged_remote_effects_enabled ? "fjärrskrivning på" : "fjärrskrivning av",
    snapshot.safe_launch.recall_http_mounted ? "Recall monterad" : "Recall av",
  ];
  if (snapshot.first_cycle_go) parts.push("GO (oväntat)");
  return parts.join(" · ");
}
