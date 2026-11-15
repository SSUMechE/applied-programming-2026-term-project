/* Offline WebGL2 depth renderer. No simulation, mesh modification or dependency.
 *
 * AP_VIEWER_DEPTH.create({canvas?}) creates an independent renderer. The optional
 * canvas must be unused by another context. render(frame) returns that canvas.
 * frame = {width,height,cssWidth,cssHeight,primitives}. width/height are physical
 * integer pixels. Geometry X/Y, bead radius and lineWidth are CSS pixels. Each
 * point is [X,Y,depth] with larger depth nearer the camera. Depth and depthRadius
 * use the same world unit, normally metres. Colors are straight RGBA in [0,1].
 *
 * A convex face has {id,points,color,stroke?,lineWidth?,depthBias?,pickable?}.
 * A line has two points, optional kind:'line', and lineWidth (default 1).
 * A bead has {id,kind:'bead',center,radius,depthRadius,color,pickable?}.
 * depthBias defaults to zero. A positive value moves the surface nearer for
 * explicitly intended coplanar overlays. Strokes receive a small depth epsilon.
 * Lines default to depthWrite:false, so a route does not become physical cover.
 * Faces and beads default to depthWrite:true when opaque. Transparent surfaces
 * always depth-test without writing depth, and are composited far to near.
 *
 * pick(cssX,cssY) returns {id,index,kind}, or null. It uses a separate depth/ID
 * framebuffer and selects the nearest pickable surface, including a transparent
 * cup. pickable:false annotations are omitted from this pass. Use one renderer
 * per display/capture canvas to keep their frame and picking state independent.
 *
 * A lost/unavailable WebGL2 context throws DepthRendererError. There is no
 * painter-order fallback for opaque geometry. Context restoration rebuilds GPU
 * resources on the next render. The caller may listen on the canvas and redraw.
 * Call dispose() when a canvas is no longer used. The pure helpers exported at
 * the end of this module do not need a browser or graphics context.
 */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AP_VIEWER_DEPTH = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const STRIDE = 11;
  const MAX_PICK_ID = 0xffffff;
  class DepthRendererError extends Error {
    constructor(code, message) {
      super(message);
      this.name = 'DepthRendererError';
      this.code = code;
    }
  }
  function fail(code, message) { throw new DepthRendererError(code, message); }
  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function point(value, label) {
    if (!Array.isArray(value) || value.length !== 3 || !value.every(finite))
      fail('INVALID_FRAME', label + ' must contain three finite coordinates.');
    return value;
  }
  function color(value, label) {
    if (!Array.isArray(value) || value.length !== 4 || !value.every(v => finite(v) && v >= 0 && v <= 1))
      fail('INVALID_FRAME', label + ' must contain four RGBA values in [0, 1].');
    return value;
  }
  function validateFrame(frame) {
    if (!frame || !['width', 'height'].every(k => Number.isSafeInteger(frame[k]) && frame[k] > 0)
        || !['cssWidth', 'cssHeight'].every(k => finite(frame[k]) && frame[k] > 0))
      fail('INVALID_FRAME', 'Render dimensions must be positive pixels and CSS sizes.');
    if (!Array.isArray(frame.primitives) || frame.primitives.length > MAX_PICK_ID)
      fail('INVALID_FRAME', 'A frame needs a finite primitive list.');
    const primitives = frame.primitives.map((p, index) => {
      if (!p || typeof p !== 'object') fail('INVALID_FRAME', 'Invalid primitive at ' + index + '.');
      const kind = p.kind === 'bead' ? 'bead' : p.points?.length === 2 ? 'line' : 'face';
      color(p.color, 'Primitive color');
      if (p.stroke !== undefined) color(p.stroke, 'Stroke color');
      if (p.depthBias !== undefined && !finite(p.depthBias)) fail('INVALID_FRAME', 'depthBias must be finite.');
      if (p.lineWidth !== undefined && (!finite(p.lineWidth) || p.lineWidth < 0))
        fail('INVALID_FRAME', 'lineWidth must be nonnegative CSS pixels.');
      if (p.depthWrite !== undefined && typeof p.depthWrite !== 'boolean')
        fail('INVALID_FRAME', 'depthWrite must be boolean.');
      if (p.pickable !== undefined && typeof p.pickable !== 'boolean')
        fail('INVALID_FRAME', 'pickable must be boolean.');
      if (kind === 'bead') {
        point(p.center, 'Bead center');
        if (!finite(p.radius) || p.radius <= 0 || !finite(p.depthRadius) || p.depthRadius <= 0)
          fail('INVALID_FRAME', 'A bead needs positive screen and world depth radii.');
      } else {
        if (!Array.isArray(p.points) || p.points.length < 2)
          fail('INVALID_FRAME', 'A primitive needs at least two points.');
        p.points.forEach(v => point(v, 'Projected point'));
        if (p.kind === 'line' && p.points.length !== 2)
          fail('INVALID_FRAME', 'A line needs exactly two points.');
      }
      const depthBias = p.depthBias ?? 0;
      const depths = kind === 'bead' ? [p.center[2] - p.depthRadius, p.center[2] + p.depthRadius]
        : p.points.map(v => v[2]);
      let minimum = Infinity, maximum = -Infinity, total = 0;
      for (const value of depths) {
        const shifted = value + depthBias;
        if (!finite(shifted)) fail('INVALID_FRAME', 'Projected depth overflow.');
        minimum = Math.min(minimum, shifted);
        maximum = Math.max(maximum, shifted);
        total += shifted / depths.length;
      }
      return {...p, index, kind, depthBias, minimumDepth: minimum, maximumDepth: maximum,
        meanDepth: total, lineWidth: p.lineWidth ?? 1,
        depthWrite: p.depthWrite ?? kind !== 'line', pickable: p.pickable !== false};
    });
    return {...frame, primitives};
  }
  function computeDepthRange(primitives) {
    let minimum = Infinity, maximum = -Infinity;
    for (const p of primitives) {
      minimum = Math.min(minimum, p.minimumDepth);
      maximum = Math.max(maximum, p.maximumDepth);
    }
    if (!primitives.length) return {minimum: -1, maximum: 1, span: 2};
    const span = Math.max(1e-6, maximum - minimum);
    const margin = Math.max(1e-6, span * .02);
    const range = {minimum: minimum - margin, maximum: maximum + margin,
      span: maximum - minimum + 2 * margin};
    if (!Object.values(range).every(finite)) fail('INVALID_FRAME', 'Depth range is not representable.');
    return range;
  }
  function projectDepth(depth, range) {
    if (!finite(depth) || !range || !finite(range.minimum) || !finite(range.span) || range.span <= 0)
      fail('INVALID_FRAME', 'Invalid depth projection.');
    return 1 - 2 * (depth - range.minimum) / range.span;
  }
  function triangulateConvex(points) {
    if (!Array.isArray(points) || points.length < 3) fail('INVALID_FRAME', 'A convex face needs at least three points.');
    points.forEach(p => point(p, 'Face point'));
    const result = [];
    for (let i = 1; i < points.length - 1; i++) result.push([points[0], points[i], points[i + 1]]);
    return result;
  }
  class VertexBuffer {
    constructor() { this.values = new Float32Array(4096 * STRIDE); this.length = 0; }
    reset() { this.length = 0; }
    reserve(count) {
      if (this.length + count * STRIDE <= this.values.length) return;
      let size = this.values.length;
      while (size < this.length + count * STRIDE) size *= 2;
      const next = new Float32Array(size);
      next.set(this.values.subarray(0, this.length));
      this.values = next;
    }
    vertex(x, y, z, rgba, pickId, u = 0, v = 0, radius = 0) {
      this.reserve(1);
      const a = this.values;
      let i = this.length;
      a[i++] = x; a[i++] = y; a[i++] = z;
      a[i++] = rgba[0]; a[i++] = rgba[1]; a[i++] = rgba[2]; a[i++] = rgba[3];
      a[i++] = u; a[i++] = v; a[i++] = radius; a[i++] = pickId;
      this.length = i;
    }
    view() { return this.values.subarray(0, this.length); }
    get count() { return this.length / STRIDE; }
  }
  function appendLine(target, a, b, rgba, width, bias, pickId) {
    if (!width || !rgba[3]) return;
    const dx = b[0] - a[0], dy = b[1] - a[1], length = Math.hypot(dx, dy);
    if (length < 1e-10) return;
    const x = -dy / length * width / 2, y = dx / length * width / 2;
    const corners = [[a[0]+x,a[1]+y,a[2]+bias], [a[0]-x,a[1]-y,a[2]+bias],
      [b[0]-x,b[1]-y,b[2]+bias], [b[0]+x,b[1]+y,b[2]+bias]];
    for (const n of [0,1,2,0,2,3]) target.vertex(...corners[n], rgba, pickId);
  }
  function appendPrimitive(target, p, strokeEpsilon, strokeOnly = false) {
    const pickId = p.pickable ? p.index + 1 : 0;
    if (p.kind === 'line') {
      appendLine(target, p.points[0], p.points[1], p.color, p.lineWidth, p.depthBias, pickId);
    } else if (p.kind === 'bead') {
      const [x, y, z] = p.center;
      for (const [u, v] of [[-1,-1],[1,-1],[1,1],[-1,-1],[1,1],[-1,1]])
        target.vertex(x + u*p.radius, y + v*p.radius, z + p.depthBias, p.color, pickId, u, v, p.depthRadius);
    } else if (strokeOnly) {
      for (let i = 0; i < p.points.length; i++)
        appendLine(target, p.points[i], p.points[(i + 1) % p.points.length], p.stroke, p.lineWidth,
          p.depthBias + strokeEpsilon, pickId);
    } else if (p.color[3]) {
      for (let i = 1; i < p.points.length - 1; i++) {
        for (const v of [p.points[0], p.points[i], p.points[i + 1]])
          target.vertex(v[0], v[1], v[2] + p.depthBias, p.color, pickId);
      }
    }
  }
  const VERTEX_SHADER = `#version 300 es
precision highp float;
layout(location=0) in vec3 a_position;
layout(location=1) in vec4 a_color;
layout(location=2) in vec3 a_sphere;
layout(location=3) in float a_pick;
uniform vec2 u_cssSize;
uniform vec2 u_depth;
out vec4 v_color;
out vec3 v_sphere;
flat out float v_pick;
void main() {
  gl_Position = vec4(a_position.x / u_cssSize.x * 2.0 - 1.0,
    1.0 - a_position.y / u_cssSize.y * 2.0,
    1.0 - 2.0 * (a_position.z - u_depth.x) * u_depth.y, 1.0);
  v_color = a_color;
  v_sphere = a_sphere;
  v_pick = a_pick;
}`;
  function fragmentShader(pickPass) { return `#version 300 es
precision highp float;
in vec4 v_color;
in vec3 v_sphere;
flat in float v_pick;
uniform vec2 u_depth;
out vec4 result;
void main() {
  if (v_color.a <= 0.0) discard;
  float depth = gl_FragCoord.z;
  if (v_sphere.z > 0.0) {
    float radiusSquared = dot(v_sphere.xy, v_sphere.xy);
    if (radiusSquared > 1.0) discard;
    depth -= sqrt(max(0.0, 1.0-radiusSquared)) * v_sphere.z * u_depth.y;
  }
  gl_FragDepth = depth;
  ${pickPass ? `if (v_pick < 0.5) discard;
  float red = mod(v_pick, 256.0);
  float green = mod(floor(v_pick/256.0), 256.0);
  float blue = floor(v_pick/65536.0);
  result = vec4(red,green,blue,255.0)/255.0;` : 'result = v_color;'}
}`; }
  function compileProgram(gl, picking) {
    const shaders = [];
    const program = gl.createProgram();
    try {
      for (const [type, source] of [[gl.VERTEX_SHADER, VERTEX_SHADER], [gl.FRAGMENT_SHADER, fragmentShader(picking)]]) {
        const shader = gl.createShader(type);
        shaders.push(shader);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS))
          fail('WEBGL_SHADER', 'Depth shader compilation failed: ' + gl.getShaderInfoLog(shader));
        gl.attachShader(program, shader);
      }
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS))
        fail('WEBGL_SHADER', 'Depth program linking failed: ' + gl.getProgramInfoLog(program));
      return {program, cssSize: gl.getUniformLocation(program, 'u_cssSize'), depth: gl.getUniformLocation(program, 'u_depth')};
    } catch (error) {
      gl.deleteProgram(program);
      throw error;
    } finally { for (const shader of shaders) gl.deleteShader(shader); }
  }
  function create(options = {}) {
    const canvas = options.canvas || (typeof document !== 'undefined' ? document.createElement('canvas')
      : typeof OffscreenCanvas !== 'undefined' ? new OffscreenCanvas(1, 1) : null);
    if (!canvas || typeof canvas.getContext !== 'function')
      fail('WEBGL_UNAVAILABLE', 'An offscreen canvas with WebGL2 is required.');
    const gl = canvas.getContext('webgl2', {alpha: true, antialias: true, depth: true,
      stencil: false, premultipliedAlpha: true, preserveDrawingBuffer: true});
    if (!gl) fail('WEBGL_UNAVAILABLE', 'WebGL2 is unavailable. Enable browser graphics support to view the scene.');
    const opaque = new VertexBuffer(), translucent = new VertexBuffer();
    let resources = null, current = null, range = null, pickValid = false;
    let lost = false, disposed = false, renderCount = 0, restoreCount = 0;
    const pixel = new Uint8Array(4);
    function ensureContext() {
      if (disposed) fail('DISPOSED', 'This depth renderer has been disposed.');
      if (lost || gl.isContextLost()) fail('CONTEXT_LOST', 'The graphics context was lost. The view will redraw when it is restored.');
    }
    function onLost(event) {
      event.preventDefault();
      lost = true; pickValid = false; resources = null;
    }
    function onRestored() {
      lost = false; resources = null; pickValid = false; current = null;
      restoreCount++;
    }
    canvas.addEventListener?.('webglcontextlost', onLost, false);
    canvas.addEventListener?.('webglcontextrestored', onRestored, false);
    function initialize() {
      ensureContext();
      if (resources) return;
      const drawing = compileProgram(gl, false), picking = compileProgram(gl, true);
      resources = {drawing, picking, opaque: gl.createBuffer(), translucent: gl.createBuffer(),
        vao: gl.createVertexArray(), framebuffer: gl.createFramebuffer(),
        texture: gl.createTexture(), depthbuffer: gl.createRenderbuffer(), pickWidth: 0, pickHeight: 0};
    }
    function sizeLimits(frame) {
      const viewport = gl.getParameter(gl.MAX_VIEWPORT_DIMS);
      const maximum = Math.min(gl.getParameter(gl.MAX_TEXTURE_SIZE), gl.getParameter(gl.MAX_RENDERBUFFER_SIZE));
      if (frame.width > Math.min(maximum, viewport[0]) || frame.height > Math.min(maximum, viewport[1]))
        fail('FRAME_TOO_LARGE', 'Capture dimensions exceed this graphics device.');
    }
    function useProgram(program) {
      gl.useProgram(program.program);
      gl.uniform2f(program.cssSize, current.cssWidth, current.cssHeight);
      gl.uniform2f(program.depth, range.minimum, 1 / range.span);
    }
    function bind(buffer) {
      gl.bindVertexArray(resources.vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      for (const [location, size, start] of [[0,3,0],[1,4,3],[2,3,7],[3,1,10]]) {
        gl.enableVertexAttribArray(location);
        gl.vertexAttribPointer(location, size, gl.FLOAT, false, STRIDE * 4, start * 4);
      }
    }
    function draw(buffer, count) {
      if (!count) return;
      bind(buffer);
      gl.drawArrays(gl.TRIANGLES, 0, count);
    }
    function baseState() {
      gl.viewport(0, 0, current.width, current.height);
      gl.disable(gl.CULL_FACE);
      gl.disable(gl.SCISSOR_TEST);
      gl.disable(gl.DITHER);
      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LEQUAL);
      gl.depthMask(true);
      gl.colorMask(true, true, true, true);
      gl.clearDepth(1);
      gl.clearColor(0, 0, 0, 0);
    }
    function render(frame) {
      ensureContext();
      const checked = validateFrame(frame);
      initialize();
      sizeLimits(checked);
      current = checked;
      range = computeDepthRange(current.primitives);
      if (canvas.width !== current.width) canvas.width = current.width;
      if (canvas.height !== current.height) canvas.height = current.height;
      opaque.reset(); translucent.reset(); pickValid = false;
      const strokeEpsilon = range.span * 4 / 16777216;
      const transparentItems = [];
      for (const p of current.primitives) {
        if (p.color[3] > 0) {
          if (p.color[3] === 1 && p.depthWrite) appendPrimitive(opaque, p, strokeEpsilon);
          else transparentItems.push({primitive: p, stroke: false, depth: p.meanDepth});
        }
        if (p.kind === 'face' && p.stroke?.[3] > 0 && p.lineWidth > 0) {
          if (p.stroke[3] === 1 && p.depthWrite) appendPrimitive(opaque, p, strokeEpsilon, true);
          else transparentItems.push({primitive: p, stroke: true, depth: p.meanDepth + strokeEpsilon});
        }
      }
      // Only translucent/no-depth-write surfaces need a compositing order.
      // Opaque coverage is always decided by the per-fragment depth buffer.
      transparentItems.sort((a, b) => a.depth - b.depth);
      for (const item of transparentItems) appendPrimitive(translucent, item.primitive, strokeEpsilon, item.stroke);
      gl.bindBuffer(gl.ARRAY_BUFFER, resources.opaque);
      gl.bufferData(gl.ARRAY_BUFFER, opaque.view(), gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, resources.translucent);
      gl.bufferData(gl.ARRAY_BUFFER, translucent.view(), gl.DYNAMIC_DRAW);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      baseState();
      gl.disable(gl.BLEND);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      useProgram(resources.drawing);
      draw(resources.opaque, opaque.count);
      gl.enable(gl.BLEND);
      gl.blendEquation(gl.FUNC_ADD);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.depthMask(false);
      draw(resources.translucent, translucent.count);
      gl.depthMask(true);
      gl.disable(gl.BLEND);
      ensureContext();
      const error = gl.getError();
      if (error !== gl.NO_ERROR) fail('WEBGL_RENDER', 'Depth rendering failed with graphics error ' + error + '.');
      renderCount++;
      return canvas;
    }
    function preparePick() {
      if (pickValid) return;
      if (resources.pickWidth !== current.width || resources.pickHeight !== current.height) {
        gl.bindTexture(gl.TEXTURE_2D, resources.texture);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, current.width, current.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
        gl.bindRenderbuffer(gl.RENDERBUFFER, resources.depthbuffer);
        gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT24, current.width, current.height);
        gl.bindFramebuffer(gl.FRAMEBUFFER, resources.framebuffer);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, resources.texture, 0);
        gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, resources.depthbuffer);
        if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE)
          fail('WEBGL_PICK', 'The graphics device could not create a depth picking buffer.');
        resources.pickWidth = current.width; resources.pickHeight = current.height;
      } else gl.bindFramebuffer(gl.FRAMEBUFFER, resources.framebuffer);
      baseState();
      gl.disable(gl.BLEND);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      useProgram(resources.picking);
      draw(resources.opaque, opaque.count);
      draw(resources.translucent, translucent.count);
      pickValid = true;
    }
    function pick(cssX, cssY) {
      ensureContext();
      if (!current || !finite(cssX) || !finite(cssY) || cssX < 0 || cssY < 0
          || cssX >= current.cssWidth || cssY >= current.cssHeight) return null;
      try {
        preparePick();
        gl.bindFramebuffer(gl.FRAMEBUFFER, resources.framebuffer);
        gl.readBuffer(gl.COLOR_ATTACHMENT0);
        const x = Math.floor(cssX * current.width / current.cssWidth);
        const y = current.height - 1 - Math.floor(cssY * current.height / current.cssHeight);
        gl.readPixels(x, y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
        ensureContext();
        const error = gl.getError();
        if (error !== gl.NO_ERROR) fail('WEBGL_PICK', 'Depth picking failed with graphics error ' + error + '.');
        const index = pixel[0] + pixel[1] * 256 + pixel[2] * 65536 - 1;
        const p = current.primitives[index];
        return p ? {id: p.id ?? null, index, kind: p.kind} : null;
      } finally { if (!gl.isContextLost()) gl.bindFramebuffer(gl.FRAMEBUFFER, null); }
    }
    function getStatus() {
      return {ready: !!current && !lost && !disposed && !gl.isContextLost(),
        state: disposed ? 'disposed' : lost || gl.isContextLost() ? 'lost' : current ? 'ready' : 'awaiting-frame',
        renderCount, restoreCount, pickReady: pickValid, width: canvas.width, height: canvas.height,
        backend: 'WebGL2 per-fragment depth'};
    }
    function dispose() {
      if (disposed) return;
      if (resources && !gl.isContextLost()) {
        gl.deleteProgram(resources.drawing.program); gl.deleteProgram(resources.picking.program);
        gl.deleteBuffer(resources.opaque); gl.deleteBuffer(resources.translucent);
        gl.deleteVertexArray(resources.vao); gl.deleteFramebuffer(resources.framebuffer);
        gl.deleteTexture(resources.texture); gl.deleteRenderbuffer(resources.depthbuffer);
      }
      canvas.removeEventListener?.('webglcontextlost', onLost, false);
      canvas.removeEventListener?.('webglcontextrestored', onRestored, false);
      resources = null; current = null; disposed = true; pickValid = false;
    }
    initialize();
    return {canvas, render, pick, getStatus, dispose};
  }
  return {create, DepthRendererError, validateFrame, computeDepthRange, projectDepth, triangulateConvex};
});
