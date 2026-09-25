import { useState, useEffect, useRef, useCallback } from 'react';
import { MatchState } from './Renderer';
import { getFighterVisualFixture } from './visualFixtures';

export function useFighter() {
  const visualFixture = import.meta.env.MODE === 'visual-test'
    ? getFighterVisualFixture(new URLSearchParams(window.location.search).get('visualState'))
    : null;
  const [gameState, setGameState] = useState<MatchState | null>(visualFixture?.state ?? null);
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  
  const wsRef = useRef<WebSocket | null>(null);
  const credentialsRef = useRef<{ matchId: string; playerId: string; sessionToken: string } | null>(null);
  const reconnectTimeout = useRef<NodeJS.Timeout | null>(null);
  const backoff = useRef(1000);
  const [credentials, setCredentials] = useState<{ matchId: string; playerId: string; sessionToken: string } | null>(
    visualFixture?.credentials ?? null,
  );

  const connectWS = useCallback((matchId: string, playerId: string, sessionToken: string) => {
    if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
    }
    setConnecting(true);
    
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/fighter-ws/${matchId}`;
    
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;
    
    ws.onopen = () => {
      setConnecting(false);
      backoff.current = 1000;
      ws.send(JSON.stringify({ type: 'authenticate', player_id: playerId, session_token: sessionToken }));
      ws.send(JSON.stringify({ type: 'sync' }));
    };
    
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'match_state' && msg.match) {
          setGameState(msg.match);
          setError(null);
        } else if (msg.type === 'error') {
          setError(msg.detail);
        }
      } catch (err) {}
    };
    
    ws.onclose = () => {
      setConnecting(true);
      reconnectTimeout.current = setTimeout(() => {
        if (credentialsRef.current) {
          backoff.current = Math.min(backoff.current * 1.5, 10000);
          connectWS(credentialsRef.current.matchId, credentialsRef.current.playerId, credentialsRef.current.sessionToken);
        }
      }, backoff.current);
    };
  }, []);

  const createMatch = async (displayName: string) => {
    if (visualFixture) return;
    try {
      setConnecting(true);
      setError(null);
      const res = await fetch('/fighter-api/matches/ai', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_name: displayName })
      });
      if (!res.ok) {
          const errBody = await res.json().catch(()=>({}));
          throw new Error(errBody.detail || 'Failed to initialize match');
      }
      const data = await res.json();
      
      const creds = {
        matchId: data.match.match_id,
        playerId: data.player_session.player_id,
        sessionToken: data.player_session.session_token
      };
      credentialsRef.current = creds;
      setCredentials(creds);
      setGameState(data.match);
      connectWS(creds.matchId, creds.playerId, creds.sessionToken);
    } catch (err: any) {
      setError(err.message);
      setConnecting(false);
    }
  };
  
  const selectCharacter = useCallback((characterId: string) => {
    if (visualFixture) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) {
       wsRef.current.send(JSON.stringify({ type: 'select_character', character_id: characterId }));
    }
  }, [visualFixture]);

  const sendAction = useCallback((action: string) => {
    if (visualFixture) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'player_input', action }));
    }
  }, [visualFixture]);

  useEffect(() => {
    return () => {
      if (wsRef.current) {
         wsRef.current.onclose = null;
         wsRef.current.close();
      }
      if (reconnectTimeout.current) clearTimeout(reconnectTimeout.current);
    };
  }, []);

  return { gameState, error, connecting, createMatch, sendAction, selectCharacter, credentials };
}
