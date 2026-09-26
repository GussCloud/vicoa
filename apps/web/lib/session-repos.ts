/**
 * New-session Git picker: start a session on one or more GitHub repositories,
 * each on a branch of the user's choosing.
 *
 * The daemon does the work (`vicoa/rpc/repo_ops.py`): it lists what the
 * machine's `gh` login can reach, clones each pick once into a managed base
 * and adds a per-session worktree on the chosen (or a newly cut) branch. The
 * first pick marked primary becomes the session's cwd; the rest reach Claude
 * as `additional_directories` (`--add-dir`).
 */

import { getRpcClient, RpcError } from '@/lib/ws-client';

type MetadataShape = {
  metadata?: Record<string, unknown> | null;
  machine_metadata?: Record<string, unknown> | null;
};

/** Whether the machine's daemon serves the picker's RPCs (see `_capabilities`). */
export function machineSupportsSessionRepos(machine: MetadataShape | null | undefined): boolean {
  if (!machine) return false;
  const meta = machine.metadata ?? machine.machine_metadata;
  const caps = meta && typeof meta === 'object' ? (meta as Record<string, unknown>).capabilities : undefined;
  return Array.isArray(caps) && caps.some((c) => String(c) === 'session-repos');
}

export interface GithubRepo {
  full_name: string;
  private: boolean;
  default_branch: string;
  description: string;
  pushed_at: string | null;
  can_push: boolean;
  cloned: boolean;
}

export interface GithubBranches {
  default_branch: string;
  branches: string[];
}

/** One repository chosen in the picker. */
export interface SessionRepoPick {
  full_name: string;
  /** Existing remote branch to check out, or the base of `new_branch`. */
  branch: string;
  /** When set, a new branch with this name is cut from `branch`. */
  new_branch?: string;
  /** The session runs in this repo; exactly one pick is primary. */
  primary: boolean;
}

export interface PreparedRepo {
  full_name: string;
  branch: string;
  base_branch: string;
  state: 'pending' | 'running' | 'done' | 'error' | 'skipped' | 'rolled_back';
  path?: string | null;
  repo_root?: string;
  error?: string;
}

export interface PrepareJob {
  job_id: string;
  state: 'running' | 'done' | 'error';
  error?: string;
  repos: PreparedRepo[];
}

/** Human copy for the daemon's `gh` error codes. */
export function describeRepoError(code: string): string {
  switch (code) {
    case 'gh_missing':
      return 'The GitHub CLI (gh) is not installed on this machine.';
    case 'gh_unauthenticated':
      return 'gh is not signed in on this machine — run `gh auth login` there.';
    case 'gh_unavailable':
      return 'GitHub could not be reached from this machine. Try again.';
    case 'not_found':
      return 'Repository not found, or the token cannot see it.';
    default:
      return code;
  }
}

async function call(machineId: string, method: string, params: Record<string, unknown>) {
  const result = await getRpcClient(machineId).callRpc(machineId, method, params);
  if (typeof result.error === 'string') throw new RpcError(result.error);
  return result;
}

export async function rpcGithubRepoList(machineId: string, refresh = false): Promise<GithubRepo[]> {
  const result = await call(machineId, 'github-repo-list', { refresh });
  return (result.repos as GithubRepo[]) ?? [];
}

export async function rpcGithubBranchList(machineId: string, fullName: string): Promise<GithubBranches> {
  const result = await call(machineId, 'github-branch-list', { full_name: fullName });
  return {
    default_branch: String(result.default_branch ?? ''),
    branches: Array.isArray(result.branches) ? (result.branches as string[]) : [],
  };
}

const POLL_INTERVAL_MS = 1000;
// Generous: a first clone of a large repo over a slow link takes minutes.
const PREPARE_TIMEOUT_MS = 20 * 60 * 1000;

/**
 * Prepare every pick on the machine and resolve the spawn inputs.
 *
 * Throws an Error with a readable message when any repo fails (the daemon has
 * already rolled back the others). `onProgress` receives each polled job.
 */
export async function prepareSessionRepos(
  machineId: string,
  picks: SessionRepoPick[],
  onProgress?: (job: PrepareJob) => void,
): Promise<{ directory: string; additionalDirectories: string[] }> {
  const ordered = [...picks].sort((a, b) => Number(b.primary) - Number(a.primary));
  const started = await call(machineId, 'repo-prepare', {
    repos: ordered.map((p) => ({
      full_name: p.full_name,
      branch: p.branch,
      ...(p.new_branch?.trim() ? { new_branch: p.new_branch.trim() } : {}),
    })),
  });
  const jobId = String(started.job_id ?? '');
  if (!jobId) throw new Error('The machine did not start preparing the repositories.');

  const deadline = Date.now() + PREPARE_TIMEOUT_MS;
  for (;;) {
    const job = (await call(machineId, 'repo-prepare-status', { job_id: jobId })) as unknown as PrepareJob;
    onProgress?.(job);
    if (job.state === 'error') {
      throw new Error(job.error || 'Failed to prepare the repositories.');
    }
    if (job.state === 'done') {
      const paths = job.repos.map((r) => r.path).filter((p): p is string => typeof p === 'string' && !!p);
      if (paths.length !== ordered.length) throw new Error('A repository was not prepared.');
      return { directory: paths[0], additionalDirectories: paths.slice(1) };
    }
    if (Date.now() > deadline) throw new Error('Preparing the repositories timed out.');
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
}

/** Short chip label for the current picks. */
export function sessionReposLabel(picks: SessionRepoPick[]): string {
  if (picks.length === 0) return 'Git repos';
  const primary = picks.find((p) => p.primary) ?? picks[0];
  const name = primary.full_name.split('/').pop() ?? primary.full_name;
  const branch = primary.new_branch?.trim() || primary.branch;
  const rest = picks.length - 1;
  return `${name}@${branch}${rest > 0 ? ` +${rest}` : ''}`;
}
