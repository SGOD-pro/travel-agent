"use client";

import React, { useState, useEffect, useRef } from "react";
import { Mic, MicOff, Volume2, VolumeX, Play, Square, Pause } from "lucide-react";

interface VoiceBriefAssistantProps {
  onTranscript?: (text: string) => void;
  narrativeText?: string;
}

// Minimal Web Speech API typing
interface SpeechRecognitionEvent {
  resultIndex: number;
  results: {
    [index: number]: {
      [index: number]: {
        transcript: string;
      };
      isFinal: boolean;
    };
    length: number;
  };
}

interface SpeechRecognitionInstance {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: (event: SpeechRecognitionEvent) => void;
  onerror: (event: unknown) => void;
  onend: () => void;
}

export default function VoiceBriefAssistant({
  onTranscript,
  narrativeText,
}: VoiceBriefAssistantProps) {
  const [isListening, setIsListening] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [isPaused, setIsPaused] = useState(false);
  const [hasSpeechSupport, setHasSpeechSupport] = useState(false);
  const [interimText, setInterimText] = useState("");

  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null);

  useEffect(() => {
    // Check speech recognition support
    if (typeof window !== "undefined") {
      const windowObj = window as unknown as {
        SpeechRecognition?: new () => SpeechRecognitionInstance;
        webkitSpeechRecognition?: new () => SpeechRecognitionInstance;
      };

      const SpeechRec = windowObj.SpeechRecognition || windowObj.webkitSpeechRecognition;
      if (SpeechRec) {
        setHasSpeechSupport(true);
        const recognizer = new SpeechRec();
        recognizer.continuous = true;
        recognizer.interimResults = true;
        recognizer.lang = "en-IN"; // Indian English

        recognizer.onresult = (event: SpeechRecognitionEvent) => {
          let currentInterim = "";
          let finalTranscript = "";

          for (let i = event.resultIndex; i < event.results.length; ++i) {
            if (event.results[i].isFinal) {
              finalTranscript += event.results[i][0].transcript + " ";
            } else {
              currentInterim += event.results[i][0].transcript;
            }
          }

          if (currentInterim) {
            setInterimText(currentInterim);
          }

          if (finalTranscript && onTranscript) {
            onTranscript(finalTranscript.trim());
            setInterimText("");
          }
        };

        recognizer.onerror = () => {
          setIsListening(false);
        };

        recognizer.onend = () => {
          setIsListening(false);
        };

        recognitionRef.current = recognizer;
      }
    }

    return () => {
      if (typeof window !== "undefined" && window.speechSynthesis) {
        window.speechSynthesis.cancel();
      }
      if (recognitionRef.current) {
        try {
          recognitionRef.current.abort();
        } catch {
          // ignore
        }
      }
    };
  }, [onTranscript]);

  const toggleListening = () => {
    if (!recognitionRef.current) return;

    if (isListening) {
      recognitionRef.current.stop();
      setIsListening(false);
    } else {
      setInterimText("");
      try {
        recognitionRef.current.start();
        setIsListening(true);
      } catch {
        setIsListening(false);
      }
    }
  };

  const playNarration = () => {
    if (typeof window === "undefined" || !window.speechSynthesis || !narrativeText) return;

    if (isPaused) {
      window.speechSynthesis.resume();
      setIsPaused(false);
      setIsSpeaking(true);
      return;
    }

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(narrativeText);
    utterance.rate = 0.95; // Natural conversational tempo
    utterance.pitch = 1.0;

    utterance.onend = () => {
      setIsSpeaking(false);
      setIsPaused(false);
    };

    utterance.onerror = () => {
      setIsSpeaking(false);
      setIsPaused(false);
    };

    window.speechSynthesis.speak(utterance);
    setIsSpeaking(true);
    setIsPaused(false);
  };

  const pauseNarration = () => {
    if (typeof window !== "undefined" && window.speechSynthesis) {
      window.speechSynthesis.pause();
      setIsPaused(true);
      setIsSpeaking(false);
    }
  };

  const stopNarration = () => {
    if (typeof window !== "undefined" && window.speechSynthesis) {
      window.speechSynthesis.cancel();
      setIsSpeaking(false);
      setIsPaused(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-3">
      {/* Voice Dictation (Speech-to-Text) Button */}
      {hasSpeechSupport && (
        <button
          type="button"
          onClick={toggleListening}
          className={`flex items-center gap-2 px-3.5 py-1.5 rounded-full text-xs font-semibold transition-all ${
            isListening
              ? "bg-red-500/20 text-red-400 border border-red-500/40 animate-pulse"
              : "bg-[#15271F] text-[#A9B8AD] border border-[#233E32] hover:border-[#B7C9AD] hover:text-[#F7F7F2]"
          }`}
          title={isListening ? "Listening... Click to stop" : "Speak travel brief with voice"}
        >
          {isListening ? (
            <>
              <MicOff className="w-3.5 h-3.5 text-red-400 animate-bounce" />
              <span>Listening...</span>
            </>
          ) : (
            <>
              <Mic className="w-3.5 h-3.5 text-[#B7C9AD]" />
              <span>Voice Dictation</span>
            </>
          )}
        </button>
      )}

      {/* Interim Transcribed Speech Preview */}
      {isListening && interimText && (
        <span className="text-xs text-[#B7C9AD] italic truncate max-w-xs animate-fade-in">
          &quot;{interimText}&quot;
        </span>
      )}

      {/* Audio Tour-Guide Narrator (Text-to-Speech) */}
      {narrativeText && (
        <div className="flex items-center gap-1.5 bg-[#15271F] px-2.5 py-1 rounded-full border border-[#233E32]">
          <span className="text-[11px] text-[#A9B8AD] font-medium mr-1 flex items-center gap-1">
            <Volume2 className="w-3 h-3 text-[#B7C9AD]" /> Audio Guide:
          </span>

          {!isSpeaking && !isPaused ? (
            <button
              type="button"
              onClick={playNarration}
              className="p-1 rounded-full text-[#B7C9AD] hover:bg-[#1B3329] transition-colors"
              title="Play audio itinerary narration"
            >
              <Play className="w-3.5 h-3.5 fill-current" />
            </button>
          ) : isSpeaking ? (
            <button
              type="button"
              onClick={pauseNarration}
              className="p-1 rounded-full text-[#B7C9AD] hover:bg-[#1B3329] transition-colors"
              title="Pause narration"
            >
              <Pause className="w-3.5 h-3.5 fill-current" />
            </button>
          ) : (
            <button
              type="button"
              onClick={playNarration}
              className="p-1 rounded-full text-[#B7C9AD] hover:bg-[#1B3329] transition-colors"
              title="Resume narration"
            >
              <Play className="w-3.5 h-3.5 fill-current" />
            </button>
          )}

          {(isSpeaking || isPaused) && (
            <button
              type="button"
              onClick={stopNarration}
              className="p-1 rounded-full text-[#A9B8AD] hover:text-red-400 transition-colors"
              title="Stop narration"
            >
              <Square className="w-3 h-3 fill-current" />
            </button>
          )}
        </div>
      )}
    </div>
  );
}
