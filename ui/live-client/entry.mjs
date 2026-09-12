import {X2Model} from '@reactor-models/x2/core';
import {createLiveController} from './controller.mjs';

globalThis.ReactorLive = Object.freeze({
  version: '1.0.0',
  create: callbacks => createLiveController(X2Model, callbacks),
});
