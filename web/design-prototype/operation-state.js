/* UI-only state. This module never sends a command or confirms a physical stop. */
(function (root) {
  class OperationState {
    constructor() {
      this.preview = true;
      this.stale = false;
      this.hasControl = false;
      this.estopPending = false;
      this.mission = 'running';
      this.missionNumber = 1;
      this.command = 'STOP';
    }
    get blocked() { return !this.preview || this.stale || this.estopPending; }
    get canStart() { return !this.blocked && !this.hasControl && this.mission !== 'running'; }
    get canToggleMission() { return !this.blocked && !this.hasControl; }
    stop() { this.command = 'STOP'; }
    suspend() { this.stop(); this.hasControl = false; this.mission = 'paused'; }
    setPreview(enabled) {
      if (this.preview === enabled) return;
      this.suspend();
      this.preview = enabled;
    }
    setStale(stale) { this.suspend(); this.stale = stale; }
    claim() {
      if (this.blocked) return false;
      this.stop(); this.mission = 'paused'; this.hasControl = true;
      return true;
    }
    release() { this.stop(); this.hasControl = false; }
    move(command) {
      if (this.blocked || !this.hasControl) return false;
      if (!['FORWARD', 'BACK', 'LEFT', 'RIGHT', 'STOP'].includes(command)) return false;
      this.command = command;
      return true;
    }
    toggleMission() {
      if (!this.canToggleMission) return false;
      this.mission = this.mission === 'running' ? 'paused' : 'running';
      return true;
    }
    startMission() {
      if (!this.canStart) return false;
      this.missionNumber = 2;
      this.mission = 'running';
      return true;
    }
    requestEstop() { this.suspend(); this.estopPending = true; }
    resetPreview() {
      this.suspend(); this.preview = true; this.stale = false; this.estopPending = false;
      this.missionNumber = 1;
    }
  }
  root.OperationState = OperationState;
  if (typeof module !== 'undefined' && module.exports) module.exports = OperationState;
})(globalThis);
