"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Browser-native voice I/O (Web Speech API) — no server round-trip, no API key, no cost.
 * Support varies by browser (notably: no SpeechRecognition in Firefox as of writing), so
 * every consumer must check `recognitionSupported`/`synthesisSupported` before offering
 * the corresponding control, rather than assuming both are always available.
 */
export function useVoice() {
  const recognitionRef = useRef<any>(null);
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [recognitionSupported, setRecognitionSupported] = useState(false);
  const [synthesisSupported, setSynthesisSupported] = useState(false);

  useEffect(() => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    setRecognitionSupported(!!SpeechRecognition);
    setSynthesisSupported(typeof window !== "undefined" && "speechSynthesis" in window);
  }, []);

  const startListening = useCallback((onResult: (text: string) => void, onError?: () => void) => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SpeechRecognition) return;

    const recognition = new SpeechRecognition();
    recognition.lang = "sv-SE";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;

    recognition.onresult = (event: any) => {
      const text = event.results?.[0]?.[0]?.transcript ?? "";
      if (text) onResult(text);
    };
    recognition.onerror = () => {
      setListening(false);
      onError?.();
    };
    recognition.onend = () => setListening(false);

    recognitionRef.current = recognition;
    setListening(true);
    recognition.start();
  }, []);

  const stopListening = useCallback(() => {
    recognitionRef.current?.stop();
    setListening(false);
  }, []);

  const speak = useCallback((text: string, onEnd?: () => void) => {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel(); // never overlap utterances
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "sv-SE";
    utterance.onstart = () => setSpeaking(true);
    utterance.onend = () => {
      setSpeaking(false);
      onEnd?.();
    };
    utterance.onerror = () => {
      setSpeaking(false);
      onEnd?.();
    };
    window.speechSynthesis.speak(utterance);
  }, []);

  const cancelSpeaking = useCallback(() => {
    if (typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
    setSpeaking(false);
  }, []);

  return {
    recognitionSupported,
    synthesisSupported,
    listening,
    speaking,
    startListening,
    stopListening,
    speak,
    cancelSpeaking,
  };
}
