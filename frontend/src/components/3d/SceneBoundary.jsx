import { Component } from 'react';

// Contains WebGL / R3F runtime failures (no GPU, lost context, bad model) so the 2D dashboard keeps working.
export default class SceneBoundary extends Component {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error) {
    console.warn('[SceneBoundary] 3D view disabled:', error);
  }

  render() {
    if (this.state.failed) return this.props.fallback ?? null;
    return this.props.children;
  }
}
