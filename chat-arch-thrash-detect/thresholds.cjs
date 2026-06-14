/**
 * Thrash-detector thresholds.
 *
 * Duplicate of `THRESHOLDS.thrash` from
 *   chat-arch/packages/analysis/src/thresholds.ts
 * — duplicated rather than imported so the hook stays hermetic (no
 * chat-arch checkout required on the box running Claude Code).
 *
 * Canonical source: `THRESHOLDS.thrash`. When that block changes,
 * mirror the change here. Tests in this directory pin the constants
 * by value so a silent drift will surface.
 */

'use strict';

module.exports = {
  rollingWindow: 30,
  editThrashMinSameFile: 4,
  editThrashWindow: 10,
  readLoopMinSameFile: 6,
  readLoopWindow: 12,
  testLoopMinConsecutive: 3,
  toolFlailDistinctTools: 5,
  toolFlailWindow: 6,
  cooldownMinutes: 5,
};
