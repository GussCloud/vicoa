import { describe, test, expect } from 'vitest';
import { machineSupportsSessionRepos, sessionReposLabel } from './session-repos';

describe('machineSupportsSessionRepos', () => {
  test('requires the explicit capability', () => {
    expect(machineSupportsSessionRepos({ metadata: { capabilities: ['worktree', 'session-repos'] } })).toBe(true);
    expect(machineSupportsSessionRepos({ machine_metadata: { capabilities: ['session-repos'] } })).toBe(true);
    expect(machineSupportsSessionRepos({ metadata: { capabilities: ['worktree'] } })).toBe(false);
    expect(machineSupportsSessionRepos({ metadata: {} })).toBe(false);
    expect(machineSupportsSessionRepos(null)).toBe(false);
  });
});

describe('sessionReposLabel', () => {
  test('names the primary repo, its branch and the count of others', () => {
    expect(sessionReposLabel([])).toBe('Git repos');
    expect(
      sessionReposLabel([
        { full_name: 'acme/web', branch: 'main', primary: false },
        { full_name: 'acme/api', branch: 'develop', new_branch: 'feat/x', primary: true },
      ]),
    ).toBe('api@feat/x +1');
    expect(sessionReposLabel([{ full_name: 'acme/api', branch: 'main', primary: true }])).toBe('api@main');
  });
});
