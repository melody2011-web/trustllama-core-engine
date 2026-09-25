import { useCallback, useEffect, useRef, useState, type TouchEvent as ReactTouchEvent } from 'react';
import { Circle } from 'lucide-react';
import { useArcadeEngine } from '@/hooks/use-arcade-engine';

type Axis = { x: number; y: number };

export function normalizedAxis(clientX: number, clientY: number, element: HTMLElement): Axis {
  const rect = element.getBoundingClientRect();
  if (!rect.width || !rect.height) return { x: 0, y: 0 };
  // client coordinates and DOM bounds are both CSS pixels. DPR scales only
  // the backing canvas and must never be applied to control coordinates.
  const dx = (clientX - (rect.left + rect.width / 2)) / (rect.width / 2);
  const dy = (clientY - (rect.top + rect.height / 2)) / (rect.height / 2);
  const length = Math.hypot(dx, dy);
  if (length <= 1) return { x: dx, y: dy };
  return { x: dx / length, y: dy / length };
}

export function keyboardAxis(held: ReadonlySet<string>): Axis {
  return {
    x: (held.has('ArrowRight') || held.has('KeyD') ? 1 : 0)
      - (held.has('ArrowLeft') || held.has('KeyA') ? 1 : 0),
    y: (held.has('ArrowDown') || held.has('KeyS') ? 1 : 0)
      - (held.has('ArrowUp') || held.has('KeyW') ? 1 : 0),
  };
}

export function TouchControls() {
  const { actions, player } = useArcadeEngine();
  const padRef = useRef<HTMLDivElement>(null);
  const pointerId = useRef<number | null>(null);
  const touchId = useRef<number | null>(null);
  const actionPointerId = useRef<number | null>(null);
  const actionTouchId = useRef<number | null>(null);
  const axisRef = useRef<Axis>({ x: 0, y: 0 });
  const [axis, setAxis] = useState<Axis>({ x: 0, y: 0 });

  const updateAxis = useCallback((next: Axis, jump = false) => {
    axisRef.current = next;
    setAxis(next);
    actions.directionalAction(next, jump);
  }, [actions]);

  const release = useCallback(() => updateAxis({ x: 0, y: 0 }), [updateAxis]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (document.hidden) return;
      const current = axisRef.current;
      if (current.x !== 0 || current.y !== 0) actions.directionalAction(current);
    }, 50);
    return () => window.clearInterval(interval);
  }, [actions]);

  useEffect(() => {
    const held = new Set<string>();
    const sendKeyboardAxis = (jump = false) => {
      updateAxis(keyboardAxis(held), jump);
    };
    const down = (event: KeyboardEvent) => {
      if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'KeyW', 'KeyA', 'KeyS', 'KeyD'].includes(event.code)) {
        event.preventDefault();
        held.add(event.code);
        sendKeyboardAxis(event.code === 'ArrowUp' || event.code === 'KeyW');
      }
      if ((event.code === 'Space' || event.code === 'Enter') && !event.repeat) {
        event.preventDefault();
        actions.primaryAction(true);
      }
    };
    const up = (event: KeyboardEvent) => {
      held.delete(event.code);
      sendKeyboardAxis();
      if (event.code === 'Space' || event.code === 'Enter') actions.primaryAction(false);
    };
    const clear = () => {
      held.clear();
      release();
      actions.primaryAction(false);
    };
    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    window.addEventListener('blur', clear);
    window.addEventListener('pagehide', clear);
    document.addEventListener('visibilitychange', clear);
    return () => {
      window.removeEventListener('keydown', down);
      window.removeEventListener('keyup', up);
      window.removeEventListener('blur', clear);
      window.removeEventListener('pagehide', clear);
      document.removeEventListener('visibilitychange', clear);
    };
  }, [actions, release, updateAxis]);

  if (!player || player.game_over) return null;

  const movePointer = (clientX: number, clientY: number) => {
    if (!padRef.current) return;
    const next = normalizedAxis(clientX, clientY, padRef.current);
    updateAxis(next, next.y < -0.65);
  };

  const findTouch = (event: ReactTouchEvent, id: number) =>
    Array.from(event.changedTouches).find((contact) => contact.identifier === id);

  return (
    <div className="absolute bottom-2 left-2 right-2 sm:bottom-3 sm:left-3 sm:right-3 flex justify-between items-end pointer-events-none z-20">
      {player.current_level === 2 ? (
        <div
          role="group"
          aria-label="Level 2 left and right movement"
          className="flex gap-2 pointer-events-auto"
          data-testid="controls-level2-direction"
        >
          {([-1, 1] as const).map((direction) => (
            <button
              key={direction}
              type="button"
              aria-label={direction < 0 ? 'Move left' : 'Move right'}
              data-testid={direction < 0 ? 'button-level2-left' : 'button-level2-right'}
              className="h-16 w-16 sm:h-20 sm:w-20 border-4 border-secondary bg-black/75 text-secondary font-pixel text-2xl touch-none select-none active:bg-secondary active:text-black"
              onPointerDown={(event) => {
                if (!event.isPrimary || pointerId.current !== null || touchId.current !== null) return;
                event.preventDefault();
                pointerId.current = event.pointerId;
                event.currentTarget.setPointerCapture?.(event.pointerId);
                updateAxis({ x: direction, y: 0 });
              }}
              onPointerUp={(event) => {
                if (event.pointerId !== pointerId.current) return;
                event.preventDefault();
                pointerId.current = null;
                release();
              }}
              onPointerCancel={() => {
                pointerId.current = null;
                release();
              }}
              onLostPointerCapture={() => {
                pointerId.current = null;
                release();
              }}
              onTouchStart={(event) => {
                if (pointerId.current !== null || touchId.current !== null) return;
                const contact = event.changedTouches[0];
                if (!contact) return;
                event.preventDefault();
                touchId.current = contact.identifier;
                updateAxis({ x: direction, y: 0 });
              }}
              onTouchEnd={(event) => {
                if (touchId.current === null || !findTouch(event, touchId.current)) return;
                event.preventDefault();
                touchId.current = null;
                release();
              }}
              onTouchCancel={() => {
                touchId.current = null;
                release();
              }}
            >
              {direction < 0 ? '◀' : '▶'}
            </button>
          ))}
        </div>
      ) : (
        <div
          ref={padRef}
          role="application"
          aria-label="Movement joystick"
          className="relative h-24 w-24 sm:h-32 sm:w-32 shrink-0 rounded-full border-4 border-secondary bg-black/70 pointer-events-auto select-none touch-none"
          data-testid="control-joystick"
          onPointerDown={(event) => {
            if (!event.isPrimary || pointerId.current !== null || touchId.current !== null) return;
            event.preventDefault();
            pointerId.current = event.pointerId;
            event.currentTarget.setPointerCapture?.(event.pointerId);
            movePointer(event.clientX, event.clientY);
          }}
          onPointerMove={(event) => {
            if (event.pointerId !== pointerId.current) return;
            event.preventDefault();
            movePointer(event.clientX, event.clientY);
          }}
          onPointerUp={(event) => {
            if (event.pointerId !== pointerId.current) return;
            event.preventDefault();
            pointerId.current = null;
            release();
          }}
          onPointerCancel={() => {
            pointerId.current = null;
            release();
          }}
          onLostPointerCapture={() => {
            pointerId.current = null;
            release();
          }}
          onTouchStart={(event) => {
            if (pointerId.current !== null || touchId.current !== null) return;
            const contact = event.changedTouches[0];
            if (!contact) return;
            event.preventDefault();
            touchId.current = contact.identifier;
            movePointer(contact.clientX, contact.clientY);
          }}
          onTouchMove={(event) => {
            if (touchId.current === null) return;
            const contact = findTouch(event, touchId.current);
            if (!contact) return;
            event.preventDefault();
            movePointer(contact.clientX, contact.clientY);
          }}
          onTouchEnd={(event) => {
            if (touchId.current === null || !findTouch(event, touchId.current)) return;
            event.preventDefault();
            touchId.current = null;
            release();
          }}
          onTouchCancel={() => {
            touchId.current = null;
            release();
          }}
        >
          <div className="absolute inset-1/2 h-10 w-10 -ml-5 -mt-5 rounded-full border-2 border-accent bg-secondary/70"
            style={{ transform: `translate3d(${axis.x * 35}px, ${axis.y * 35}px, 0)` }}
          />
        </div>
      )}

      <button
        type="button"
        aria-label={player.current_level === 2 ? 'Jump' : 'Primary action'}
        className="w-16 h-16 sm:w-20 sm:h-20 shrink-0 rounded-full bg-black/70 border-4 border-primary text-primary pointer-events-auto flex items-center justify-center active:bg-primary active:text-black retro-shadow-primary touch-none select-none"
        onPointerDown={(event) => {
          if (actionPointerId.current !== null || actionTouchId.current !== null) return;
          event.preventDefault();
          actionPointerId.current = event.pointerId;
          event.currentTarget.setPointerCapture?.(event.pointerId);
          actions.primaryAction(true);
        }}
        onPointerUp={(event) => {
          if (event.pointerId !== actionPointerId.current) return;
          event.preventDefault();
          actionPointerId.current = null;
          actions.primaryAction(false);
        }}
        onPointerCancel={(event) => {
          if (event.pointerId !== actionPointerId.current) return;
          actionPointerId.current = null;
          actions.primaryAction(false);
        }}
        onLostPointerCapture={(event) => {
          if (event.pointerId !== actionPointerId.current) return;
          actionPointerId.current = null;
          actions.primaryAction(false);
        }}
        onTouchStart={(event) => {
          if (actionPointerId.current !== null || actionTouchId.current !== null) return;
          const contact = event.changedTouches[0];
          if (!contact) return;
          event.preventDefault();
          actionTouchId.current = contact.identifier;
          actions.primaryAction(true);
        }}
        onTouchEnd={(event) => {
          if (actionTouchId.current === null || !findTouch(event, actionTouchId.current)) return;
          event.preventDefault();
          actionTouchId.current = null;
          actions.primaryAction(false);
        }}
        onTouchCancel={() => {
          actionTouchId.current = null;
          actions.primaryAction(false);
        }}
        data-testid="button-action-primary"
      >
        {player.current_level === 2 ? (
          <span className="font-pixel text-[10px] sm:text-xs leading-none">JUMP</span>
        ) : (
          <Circle size={30} fill="currentColor" />
        )}
      </button>
    </div>
  );
}