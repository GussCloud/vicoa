'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, GitBranch, Loader2, Lock, RefreshCw, Search, Star } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { RpcError } from '@/lib/ws-client';
import {
  describeRepoError,
  rpcGithubBranchList,
  rpcGithubRepoList,
  type GithubBranches,
  type GithubRepo,
  type SessionRepoPick,
} from '@/lib/session-repos';

export interface SessionReposDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  machineId: string;
  value: SessionRepoPick[];
  onChange: (picks: SessionRepoPick[]) => void;
  /** Only Claude reaches extra repos (`--add-dir`); other agents get one. */
  allowMultiple: boolean;
}

function errorText(err: unknown): string {
  if (err instanceof RpcError) return describeRepoError(err.code);
  return err instanceof Error ? err.message : String(err);
}

/**
 * Pick GitHub repositories (and a branch for each) to start a session on.
 * Lists what the selected machine's `gh` login can see; the daemon clones and
 * checks out on submit, not here.
 */
export function SessionReposDialog({
  open,
  onOpenChange,
  machineId,
  value,
  onChange,
  allowMultiple,
}: SessionReposDialogProps) {
  const [repos, setRepos] = useState<GithubRepo[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [picks, setPicks] = useState<SessionRepoPick[]>(value);
  const [branches, setBranches] = useState<Record<string, GithubBranches | 'loading' | { error: string }>>({});

  const loadRepos = useCallback(
    async (refresh: boolean) => {
      if (!machineId) return;
      setLoading(true);
      setError(null);
      try {
        setRepos(await rpcGithubRepoList(machineId, refresh));
      } catch (err) {
        setError(errorText(err));
      } finally {
        setLoading(false);
      }
    },
    [machineId],
  );

  const loadBranches = useCallback(
    async (fullName: string) => {
      setBranches((prev) => ({ ...prev, [fullName]: 'loading' }));
      try {
        const result = await rpcGithubBranchList(machineId, fullName);
        setBranches((prev) => ({ ...prev, [fullName]: result }));
        // A fresh pick starts on the default branch.
        setPicks((prev) =>
          prev.map((p) =>
            p.full_name === fullName && !p.branch ? { ...p, branch: result.default_branch } : p,
          ),
        );
      } catch (err) {
        setBranches((prev) => ({ ...prev, [fullName]: { error: errorText(err) } }));
      }
    },
    [machineId],
  );

  // Reset to the committed selection each time the dialog opens.
  useEffect(() => {
    if (!open) return;
    setPicks(value);
    setQuery('');
    if (repos === null) void loadRepos(false);
    for (const pick of value) {
      if (!branches[pick.full_name]) void loadBranches(pick.full_name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Switching machines invalidates everything listed.
  useEffect(() => {
    setRepos(null);
    setBranches({});
  }, [machineId]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = repos ?? [];
    const matches = q
      ? list.filter((r) => r.full_name.toLowerCase().includes(q) || r.description.toLowerCase().includes(q))
      : list;
    // Picked repos stay on top so their branch controls don't scroll away.
    const picked = new Set(picks.map((p) => p.full_name));
    return [...matches.filter((r) => picked.has(r.full_name)), ...matches.filter((r) => !picked.has(r.full_name))];
  }, [repos, query, picks]);

  const toggle = (repo: GithubRepo) => {
    const existing = picks.find((p) => p.full_name === repo.full_name);
    if (existing) {
      const rest = picks.filter((p) => p.full_name !== repo.full_name);
      if (existing.primary && rest.length > 0) rest[0] = { ...rest[0], primary: true };
      setPicks(rest);
      return;
    }
    const pick: SessionRepoPick = {
      full_name: repo.full_name,
      branch: repo.default_branch,
      primary: picks.length === 0 || !allowMultiple,
    };
    setPicks(allowMultiple ? [...picks, pick] : [pick]);
    if (!branches[repo.full_name]) void loadBranches(repo.full_name);
  };

  const update = (fullName: string, patch: Partial<SessionRepoPick>) => {
    setPicks((prev) =>
      prev.map((p) => {
        if (patch.primary && p.full_name !== fullName) return { ...p, primary: false };
        return p.full_name === fullName ? { ...p, ...patch } : p;
      }),
    );
  };

  const branchesReady = picks.every((p) => {
    const b = branches[p.full_name];
    return p.branch && b && b !== 'loading' && !('error' in b);
  });

  const apply = () => {
    onChange(picks);
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] max-w-2xl flex-col font-mono">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base">
            <GitBranch className="h-4 w-4" /> Repositories
          </DialogTitle>
          <DialogDescription className="text-xs">
            {allowMultiple
              ? 'Pick one or more repositories and a branch for each. The star marks where the session runs; the others are opened alongside it.'
              : 'This agent works in a single repository — pick one and a branch.'}
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search repositories"
              className="h-8 pl-7 text-xs"
              autoFocus
            />
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void loadRepos(true)}
            disabled={loading}
            title="Refresh"
          >
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          </Button>
        </div>

        <div className="custom-scrollbar min-h-40 flex-1 overflow-y-auto rounded-md border">
          {error ? (
            <p className="p-4 text-xs text-destructive">{error}</p>
          ) : repos === null ? (
            <div className="flex items-center gap-2 p-4 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading repositories…
            </div>
          ) : filtered.length === 0 ? (
            <p className="p-4 text-xs text-muted-foreground">No repositories match.</p>
          ) : (
            <ul className="divide-y">
              {filtered.map((repo) => {
                const pick = picks.find((p) => p.full_name === repo.full_name);
                const branchState = branches[repo.full_name];
                return (
                  <li key={repo.full_name} className={pick ? 'bg-muted/40' : ''}>
                    <button
                      type="button"
                      onClick={() => toggle(repo)}
                      className="flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left text-xs hover:bg-muted/60"
                    >
                      <span
                        className={`flex h-4 w-4 flex-shrink-0 items-center justify-center rounded border ${pick ? 'border-primary bg-primary text-primary-foreground' : ''}`}
                      >
                        {pick && <Check className="h-3 w-3" />}
                      </span>
                      <span className="flex-1 truncate">{repo.full_name}</span>
                      {repo.private && <Lock className="h-3 w-3 flex-shrink-0 text-muted-foreground" />}
                      {!repo.can_push && <span className="text-[10px] text-muted-foreground">read-only</span>}
                    </button>
                    {pick && (
                      <div className="flex flex-wrap items-center gap-2 px-3 pb-2 pl-9 text-xs">
                        {allowMultiple && (
                          <button
                            type="button"
                            title={pick.primary ? 'Session runs here' : 'Run the session here'}
                            onClick={() => update(repo.full_name, { primary: true })}
                            className="flex cursor-pointer items-center"
                          >
                            <Star
                              className={`h-3.5 w-3.5 ${pick.primary ? 'fill-yellow-500 text-yellow-500' : 'text-muted-foreground'}`}
                            />
                          </button>
                        )}
                        {branchState === 'loading' || branchState === undefined ? (
                          <span className="flex items-center gap-1 text-muted-foreground">
                            <Loader2 className="h-3 w-3 animate-spin" /> branches…
                          </span>
                        ) : 'error' in branchState ? (
                          <span className="text-destructive">{branchState.error}</span>
                        ) : (
                          <select
                            value={pick.branch}
                            onChange={(e) => update(repo.full_name, { branch: e.target.value })}
                            className="h-7 max-w-52 cursor-pointer rounded-md border bg-background px-2 text-xs"
                            title={pick.new_branch ? 'Base branch' : 'Branch'}
                          >
                            {branchState.branches.map((b) => (
                              <option key={b} value={b}>
                                {b}
                                {b === branchState.default_branch ? ' (default)' : ''}
                              </option>
                            ))}
                          </select>
                        )}
                        <Input
                          value={pick.new_branch ?? ''}
                          onChange={(e) => update(repo.full_name, { new_branch: e.target.value })}
                          placeholder="new branch (optional)"
                          className="h-7 w-48 text-xs"
                        />
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <DialogFooter className="flex items-center gap-2 sm:justify-between">
          <span className="text-xs text-muted-foreground">
            {picks.length === 0 ? 'Nothing selected' : `${picks.length} selected`}
          </span>
          <div className="flex gap-2">
            {value.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  onChange([]);
                  onOpenChange(false);
                }}
              >
                Clear
              </Button>
            )}
            <Button size="sm" onClick={apply} disabled={picks.length > 0 && !branchesReady}>
              Use {picks.length > 1 ? 'repositories' : 'repository'}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
