// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import MemoryEvidence from '../MemoryEvidence';
import { studioApi } from '../../api';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
it('shows attributed source as plain text with Beijing time', async () => {
  vi.spyOn(studioApi, 'memoryEvidence').mockResolvedValue({available:true, evidence_id:'e', group_id:'123', message_id:'m',
    sender_id:'77', sender_name:'群友', sent_at:'2026-11-20T01:02:03+00:00', text:'<script>原话</script>', archive_note:'本地归档'});
  const {container} = render(<MemoryEvidence recordKey="r" evidenceId="e" />);
  expect(await screen.findByText('<script>原话</script>')).toBeTruthy();
  expect(screen.getByText('群友（77）')).toBeTruthy();
  expect(screen.getByText(/09:02:03/)).toBeTruthy();
  expect(container.querySelector('script')).toBeNull();
});
it('shows missing source without substituting a memory summary', async () => {
  vi.spyOn(studioApi, 'memoryEvidence').mockResolvedValue({available:false, reason:'source_missing', message:'原话已删除'});
  render(<MemoryEvidence recordKey="r" evidenceId="e" />);
  expect(await screen.findByText('原话已删除')).toBeTruthy();
});
