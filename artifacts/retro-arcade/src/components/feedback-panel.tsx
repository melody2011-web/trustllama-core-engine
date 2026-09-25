import { useEffect, useState } from 'react';
import { useSubmitFeedback } from '@workspace/api-client-react';

export function FeedbackPanel() {
  const [message, setMessage] = useState('');
  const submitFeedback = useSubmitFeedback();
  const [status, setStatus] = useState<'idle' | 'submitting' | 'success' | 'error'>('idle');
  const [errorMessage, setErrorMessage] = useState('');
  
  const maxLength = 2000;

  useEffect(() => {
    if (status !== 'success') return;
    const timeout = window.setTimeout(() => setStatus('idle'), 5000);
    return () => window.clearTimeout(timeout);
  }, [status]);
  
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!message.trim() || message.length > maxLength || status === 'submitting') return;
    
    setStatus('submitting');
    
    submitFeedback.mutate(
      { data: { category: 'feedback', message, page: '/retro-arcade/' } },
      {
        onSuccess: (res) => {
          if (res.ok) {
            setStatus('success');
            setMessage('');
          } else {
            setStatus('error');
            setErrorMessage('FAILED TO TRANSMIT');
          }
        },
        onError: () => {
          setStatus('error');
          setErrorMessage('CONNECTION LOST');
        }
      }
    );
  };

  return (
    <div className="mt-8 border-t-8 border-zinc-900 pt-8 flex flex-col">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="font-mono text-muted-foreground text-sm uppercase tracking-widest flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-zinc-700"></div>
          Report Bug / Feedback
          <div className="w-2 h-2 rounded-full bg-zinc-700"></div>
        </h3>
        {status === 'success' && (
          <span className="font-pixel text-success text-glow-success text-xl animate-pulse">TRANSMITTED</span>
        )}
        {status === 'error' && (
          <span className="font-pixel text-destructive text-xl animate-pulse">{errorMessage}</span>
        )}
      </div>

      <form onSubmit={handleSubmit} className="flex flex-col md:flex-row gap-6">
        {/* Terminal Screen for Textarea */}
        <div className="flex-1 relative bg-[#0a0a0a] rounded-xl border-4 border-zinc-800 shadow-[inset_0_0_30px_rgba(0,0,0,1)] overflow-hidden group focus-within:border-zinc-700 transition-colors duration-300">
          {/* CRT effect */}
          <div className="absolute inset-0 crt-overlay pointer-events-none opacity-50 z-10" />
          <div className="absolute top-0 left-0 w-full h-1 bg-gradient-to-b from-white/10 to-transparent pointer-events-none z-10"></div>
          
          <div className="p-4 relative z-20 h-full flex flex-col min-h-[150px]">
            <div className="text-accent/50 font-mono text-xs mb-2">SYSTEM_DIAGNOSTIC_INPUT &gt;</div>
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              disabled={status === 'submitting'}
              placeholder="REPORT ANOMALIES..."
              className="flex-1 w-full bg-transparent text-accent font-mono text-sm leading-relaxed resize-none focus:outline-none placeholder:text-zinc-700 disabled:opacity-50"
              maxLength={maxLength}
              data-testid="textarea-feedback"
            />
            
            <div className="flex justify-between items-center mt-2 border-t border-zinc-800 pt-2">
              <div className="flex gap-2 items-center">
                <div className={`w-2 h-2 rounded-full ${message.length > 0 ? 'bg-accent text-glow-accent' : 'bg-zinc-800'}`}></div>
                <span className="text-zinc-600 font-mono text-xs">DATA_LINK</span>
              </div>
              <span className={`font-mono text-xs ${message.length >= maxLength ? 'text-destructive' : 'text-zinc-600'}`}>
                {message.length} / {maxLength}
              </span>
            </div>
          </div>
        </div>

        {/* Big Hardware Button */}
        <div className="w-full md:w-auto flex flex-col items-center justify-center shrink-0">
          <div className="bg-zinc-900 p-4 rounded-xl border-4 border-zinc-800 shadow-[inset_0_-4px_10px_rgba(0,0,0,0.5)] flex items-center justify-center min-w-[220px] h-full">
            <button
              type="submit"
              disabled={!message.trim() || status === 'submitting'}
              className="relative group disabled:cursor-not-allowed disabled:opacity-70"
              data-testid="button-submit-feedback"
            >
              {/* Button Shadow/Base */}
              <div className="absolute inset-0 bg-primary/20 rounded-full translate-y-2 group-active:translate-y-1 transition-transform group-disabled:translate-y-1"></div>
              <div className="absolute inset-0 bg-zinc-950 rounded-full translate-y-1.5 group-active:translate-y-0.5 transition-transform group-disabled:translate-y-0.5"></div>
              
              {/* Button Top */}
              <div className={`relative px-8 py-6 rounded-full border-4 border-primary transition-all duration-150 flex items-center justify-center
                ${status === 'submitting' ? 'bg-primary/20' : 'bg-zinc-900'}
                group-active:translate-y-1 group-disabled:translate-y-1 group-disabled:border-zinc-700
                group-hover:bg-primary/10
                retro-shadow-primary group-active:shadow-none
              `}>
                <span className={`font-pixel text-2xl tracking-widest 
                  ${status === 'submitting' ? 'text-primary/50' : 'text-primary text-glow-primary'}
                  group-disabled:text-zinc-600 group-disabled:text-shadow-none
                `}>
                  {status === 'submitting' ? 'TRANSMITTING...' : 'SUBMIT FEEDBACK'}
                </span>
              </div>
            </button>
          </div>
          <div className="text-zinc-700 font-mono text-[10px] mt-3 tracking-widest">
            CAUTION: SERVICE MODE
          </div>
        </div>
      </form>
    </div>
  );
}
