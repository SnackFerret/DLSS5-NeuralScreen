// The pointer in the GPU recording (#131).
//
// The capture never holds the pointer: Desktop Duplication hands it over
// apart from the picture, and window capture runs with it off - the overlay
// shows the processed picture under the real pointer, and a pointer baked
// into the network's input would be drawn twice, the second one a frame
// late. So a recording had no pointer at all. Drawn here, on the copy that
// goes to the encoder only: the shape of the system pointer (the same one
// the user sees, any size, colour or monochrome), at its position on the
// captured monitor or window, blended over the frame. A hidden pointer - a
// game that captures the mouse - is not drawn.
//
// The shape is read once per HCURSOR on the CPU: DrawIconEx over black and
// over white gives the colour and the coverage at once (a pixel that comes
// out differently on the two is transparent by that much), which covers
// colour, alpha and monochrome pointers alike; inverting pixels come out
// white. Premultiplied, uploaded to a small texture when it changes.

static const char kCursorHlsl[] = R"(
cbuffer CursorCB : register(b0)
{
    int2  gOrigin;   // the pointer image's top-left, in frame pixels
    uint2 gSize;     // the pointer image's size in frame pixels
    uint2 gFrame;    // the frame's size
    float gScale;    // frame pixels per screen pixel
    uint  gFlags;    // bit0 a linear (scRGB) target
};
Texture2D<float4>   gSrc    : register(t0);
Texture2D<float4>   gCursor : register(t1);
RWTexture2D<float4> gDst    : register(u0);
SamplerState        gLin    : register(s0);
[numthreads(8, 8, 1)]
void CSMain(uint3 id : SV_DispatchThreadID)
{
    if (id.x >= gSize.x || id.y >= gSize.y) return;
    int2 p = gOrigin + int2(id.xy);
    if (any(p < 0) || any(p >= int2(gFrame))) return;
    uint cw, ch;
    gCursor.GetDimensions(cw, ch);
    // The image region is gSize / gScale texels of a 256x256 texture.
    float2 uv = (float2(id.xy) + 0.5) / gScale / float2(cw, ch);
    float4 c = gCursor.SampleLevel(gLin, uv, 0);   // premultiplied
    if (c.a <= 0.0) return;
    float4 s = gSrc.Load(int3(p, 0));
    float3 rgb = c.rgb;
    if ((gFlags & 1u) != 0) rgb = pow(max(rgb, 0.0), 2.2);
    gDst[p] = float4(rgb + s.rgb * (1.0 - c.a), s.a);
}
)";

static constexpr UINT kCursorTex = 256;   // the largest pointer image kept

static struct CursorState {
    winrt::com_ptr<ID3D12RootSignature> root;
    winrt::com_ptr<ID3D12PipelineState> pso;
    winrt::com_ptr<ID3D12DescriptorHeap> heap;
    ID3D12Resource *image = nullptr;      // kCursorTex^2 RGBA8, premultiplied
    ID3D12Resource *upload = nullptr;     // its upload buffer, mapped
    BYTE *upload_ptr = nullptr;
    ID3D12Resource *target = nullptr;     // the frame with the pointer on it
    D3D12_RESOURCE_STATES target_state = D3D12_RESOURCE_STATE_COMMON;
    HCURSOR shape = nullptr;              // what the image holds
    UINT w = 0, hgt = 0;                  // its size in screen pixels
    int hot_x = 0, hot_y = 0;
    bool pending = false;                 // uploaded on the next pass
    bool image_fresh = true;              // the image texture is still in COMMON
    bool failed = false;
    bool said = false;
} g_cursor;

static void CloseCursor()
{
    auto &s = g_cursor;
    if (g_submission_failed) return;
    if ((s.image || s.target) && h.fence && h.fence_value != 0 &&
        !WaitFenceValue(h.fence, h.fence_value, 30000, "cursor-close"))
        return;
    for (ID3D12Resource **r : {&s.image, &s.upload, &s.target})
        if (*r != nullptr) { (*r)->Release(); *r = nullptr; }
    s.upload_ptr = nullptr;
    s.shape = nullptr;
    s.image_fresh = true;
    s.target_state = D3D12_RESOURCE_STATE_COMMON;
}

static bool CursorPipeline()
{
    auto &s = g_cursor;
    if (s.pso) return true;
    if (s.failed) return false;
    D3D12_DESCRIPTOR_RANGE ranges[2] = {
        {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 2, 0, 0, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, 2}};
    D3D12_ROOT_PARAMETER roots[2] = {};
    roots[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    roots[0].Constants = {0, 0, 8};
    roots[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    roots[1].DescriptorTable = {2, ranges};
    D3D12_STATIC_SAMPLER_DESC samp = {};
    samp.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
    samp.AddressU = samp.AddressV = samp.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    samp.MaxLOD = D3D12_FLOAT32_MAX;
    samp.ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    D3D12_ROOT_SIGNATURE_DESC rd = {2, roots, 1, &samp, D3D12_ROOT_SIGNATURE_FLAG_NONE};
    winrt::com_ptr<ID3DBlob> code, errors;
    if (FAILED(D3D12SerializeRootSignature(&rd, D3D_ROOT_SIGNATURE_VERSION_1, code.put(), errors.put())) ||
        FAILED(h.dev->CreateRootSignature(0, code->GetBufferPointer(), code->GetBufferSize(),
                                          IID_PPV_ARGS(s.root.put()))))
    { s.failed = true; Log("[grec] pointer root signature failed - recordings go without it"); return false; }
    code = nullptr; errors = nullptr;
    if (FAILED(D3DCompile(kCursorHlsl, sizeof(kCursorHlsl) - 1, "cursor.hlsl", nullptr, nullptr, "CSMain",
                          "cs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, code.put(), errors.put())))
    {
        s.failed = true;
        Log("[grec] pointer shader failed to compile: %s - recordings go without it",
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "?");
        return false;
    }
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = s.root.get();
    pd.CS = {code->GetBufferPointer(), code->GetBufferSize()};
    // Three descriptors a pass, eight passes round the ring: one recorded
    // frame per worker frame, and every frame waits for its predecessor.
    D3D12_DESCRIPTOR_HEAP_DESC hd = {D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 24,
                                     D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 0};
    if (FAILED(h.dev->CreateComputePipelineState(&pd, IID_PPV_ARGS(s.pso.put()))) ||
        FAILED(h.dev->CreateDescriptorHeap(&hd, IID_PPV_ARGS(s.heap.put()))))
    { s.failed = true; s.pso = nullptr; Log("[grec] pointer pipeline failed - recordings go without it"); return false; }
    return true;
}

// The pointer's shape as premultiplied RGBA into the upload buffer. Read over
// black and over white: colour = over black, coverage = 1 - (white - black).
static bool CursorReadShape(HCURSOR hc)
{
    auto &s = g_cursor;
    ICONINFO ii = {};
    if (!GetIconInfo(hc, &ii)) return false;
    BITMAP bm = {};
    UINT w = 0, hgt = 0;
    if (ii.hbmColor && GetObject(ii.hbmColor, sizeof(bm), &bm)) { w = bm.bmWidth; hgt = bm.bmHeight; }
    else if (ii.hbmMask && GetObject(ii.hbmMask, sizeof(bm), &bm)) { w = bm.bmWidth; hgt = bm.bmHeight / 2; }
    if (ii.hbmColor) DeleteObject(ii.hbmColor);
    if (ii.hbmMask) DeleteObject(ii.hbmMask);
    if (w == 0 || hgt == 0) return false;
    w = (std::min)(w, kCursorTex);
    hgt = (std::min)(hgt, kCursorTex);
    BITMAPINFO bi = {};
    bi.bmiHeader.biSize = sizeof(bi.bmiHeader);
    bi.bmiHeader.biWidth = static_cast<LONG>(w);
    bi.bmiHeader.biHeight = -static_cast<LONG>(hgt);
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    HDC screen = GetDC(nullptr);
    HDC dc = CreateCompatibleDC(screen);
    void *bits[2] = {};
    HBITMAP dib[2] = {CreateDIBSection(screen, &bi, DIB_RGB_COLORS, &bits[0], nullptr, 0),
                      CreateDIBSection(screen, &bi, DIB_RGB_COLORS, &bits[1], nullptr, 0)};
    bool ok = dib[0] && dib[1];
    for (int k = 0; ok && k < 2; ++k)
    {
        HGDIOBJ old = SelectObject(dc, dib[k]);
        RECT r = {0, 0, static_cast<LONG>(w), static_cast<LONG>(hgt)};
        FillRect(dc, &r, static_cast<HBRUSH>(GetStockObject(k == 0 ? BLACK_BRUSH : WHITE_BRUSH)));
        ok = DrawIconEx(dc, 0, 0, hc, static_cast<int>(w), static_cast<int>(hgt), 0, nullptr, DI_NORMAL) != 0;
        GdiFlush();
        SelectObject(dc, old);
    }
    if (ok)
    {
        const BYTE *black = static_cast<const BYTE *>(bits[0]);
        const BYTE *white = static_cast<const BYTE *>(bits[1]);
        const UINT pitch = kCursorTex * 4;   // a multiple of 256: a valid footprint
        memset(s.upload_ptr, 0, static_cast<size_t>(pitch) * kCursorTex);
        for (UINT y = 0; y < hgt; ++y)
            for (UINT x = 0; x < w; ++x)
            {
                const BYTE *b = black + (static_cast<size_t>(y) * w + x) * 4;   // BGRA
                const BYTE *wt = white + (static_cast<size_t>(y) * w + x) * 4;
                BYTE *o = s.upload_ptr + static_cast<size_t>(y) * pitch + static_cast<size_t>(x) * 4;
                int cover = 255;
                for (int ch = 0; ch < 3; ++ch)
                    cover = (std::min)(cover, 255 - (static_cast<int>(wt[ch]) - static_cast<int>(b[ch])));
                cover = (std::max)(0, (std::min)(255, cover));
                // An inverting pixel is white over black and black over white:
                // its coverage reads above 1, and it is drawn white.
                const bool invert = static_cast<int>(b[0]) > static_cast<int>(wt[0]);
                o[0] = invert ? 255 : b[2];   // RGBA, premultiplied (colour over black)
                o[1] = invert ? 255 : b[1];
                o[2] = invert ? 255 : b[0];
                o[3] = static_cast<BYTE>(invert ? 255 : cover);
            }
        s.w = w;
        s.hgt = hgt;
        s.hot_x = static_cast<int>(ii.xHotspot);
        s.hot_y = static_cast<int>(ii.yHotspot);
    }
    for (HBITMAP b : dib) if (b) DeleteObject(b);
    DeleteDC(dc);
    ReleaseDC(nullptr, screen);
    return ok;
}

static bool CursorResources(const D3D12_RESOURCE_DESC &frame)
{
    auto &s = g_cursor;
    if (!s.image)
    {
        s.image = MakeTex(kCursorTex, kCursorTex, DXGI_FORMAT_R8G8B8A8_UNORM, false);
        s.image_fresh = true;
        D3D12_HEAP_PROPERTIES up = {};
        up.Type = D3D12_HEAP_TYPE_UPLOAD;
        D3D12_RESOURCE_DESC bd = {};
        bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        bd.Width = static_cast<UINT64>(kCursorTex) * kCursorTex * 4;
        bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
        bd.SampleDesc.Count = 1;
        bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        if (FAILED(h.dev->CreateCommittedResource(&up, D3D12_HEAP_FLAG_NONE, &bd,
                                                  D3D12_RESOURCE_STATE_GENERIC_READ, nullptr,
                                                  IID_PPV_ARGS(&s.upload))) ||
            FAILED(s.upload->Map(0, nullptr, reinterpret_cast<void **>(&s.upload_ptr))))
        {
            CloseCursor();
            return false;
        }
    }
    if (s.target)
    {
        const auto d = s.target->GetDesc();
        if (d.Width != frame.Width || d.Height != frame.Height || d.Format != frame.Format)
        {
            if (h.fence && h.fence_value != 0 &&
                !WaitFenceValue(h.fence, h.fence_value, 30000, "cursor-resize"))
                return false;
            s.target->Release();
            s.target = nullptr;
        }
    }
    if (!s.target)
    {
        s.target = MakeTex(static_cast<UINT>(frame.Width), frame.Height, frame.Format, true);
        s.target_state = D3D12_RESOURCE_STATE_COMMON;
    }
    return s.image != nullptr && s.target != nullptr;
}

// Where the captured picture's top-left is on the screen, and its size there.
static bool CaptureScreenRect(RECT *r)
{
    if (g_wgc_active && g_wgc_hwnd != nullptr)
        return SUCCEEDED(DwmGetWindowAttribute(g_wgc_hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, r, sizeof(*r)));
    if (g_dda_active && g_capture_monitor != nullptr)
    {
        MONITORINFO mi = {sizeof(mi)};
        if (!GetMonitorInfoW(g_capture_monitor, &mi)) return false;
        *r = mi.rcMonitor;
        return true;
    }
    return false;
}

// The frame `src` (in COPY_SOURCE) with the pointer drawn on it, in
// COPY_SOURCE - or `src` itself when there is no pointer to draw.
static ID3D12Resource *CursorApply(ID3D12GraphicsCommandList *list, ID3D12Resource *src)
{
    auto &s = g_cursor;
    if (src == nullptr || list == nullptr || g_submission_failed || TestStageRequested("no-rec-cursor"))
        return src;
    CURSORINFO ci = {sizeof(ci)};
    if (!GetCursorInfo(&ci) || (ci.flags & CURSOR_SHOWING) == 0 || ci.hCursor == nullptr) return src;
    RECT area = {};
    if (!CaptureScreenRect(&area) || area.right <= area.left || area.bottom <= area.top) return src;
    const auto desc = src->GetDesc();
    // 8-bit SDR and scRGB. An HDR10 recording's PQ code values would make a
    // white pointer 10 000 nits: it goes without one.
    if (desc.Format != DXGI_FORMAT_R8G8B8A8_UNORM && desc.Format != DXGI_FORMAT_B8G8R8A8_UNORM &&
        desc.Format != DXGI_FORMAT_R16G16B16A16_FLOAT)
        return src;
    const float scale = static_cast<float>(desc.Width) / static_cast<float>(area.right - area.left);
    if (!CursorPipeline() || !CursorResources(desc)) return src;
    if (ci.hCursor != s.shape)
    {
        // The upload buffer may still be read by the last frame's copy.
        if (s.shape != nullptr && h.fence && h.fence_value != 0 &&
            !WaitFenceValue(h.fence, h.fence_value, 30000, "cursor-shape"))
            return src;
        if (!CursorReadShape(ci.hCursor)) return src;
        s.shape = ci.hCursor;
        s.pending = true;
    }
    const int ox = static_cast<int>(std::floor((ci.ptScreenPos.x - s.hot_x - area.left) * scale));
    const int oy = static_cast<int>(std::floor((ci.ptScreenPos.y - s.hot_y - area.top) * scale));
    const UINT sw = (std::max)(1u, static_cast<UINT>(std::ceil(s.w * scale)));
    const UINT sh = (std::max)(1u, static_cast<UINT>(std::ceil(s.hgt * scale)));
    if (ox >= static_cast<int>(desc.Width) || oy >= static_cast<int>(desc.Height) ||
        ox + static_cast<int>(sw) <= 0 || oy + static_cast<int>(sh) <= 0)
        return src;   // off this picture
    if (!s.said)
    {
        Log("[grec] the pointer is drawn into the recording");
        s.said = true;
    }
    if (s.pending)
    {
        D3D12_RESOURCE_BARRIER to = Transition(s.image,
            s.image_fresh ? D3D12_RESOURCE_STATE_COMMON : D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
            D3D12_RESOURCE_STATE_COPY_DEST);
        list->ResourceBarrier(1, &to);
        D3D12_TEXTURE_COPY_LOCATION dl = {};
        dl.pResource = s.image;
        dl.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        D3D12_TEXTURE_COPY_LOCATION sl = {};
        sl.pResource = s.upload;
        sl.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        sl.PlacedFootprint.Footprint = {DXGI_FORMAT_R8G8B8A8_UNORM, kCursorTex, kCursorTex, 1, kCursorTex * 4};
        list->CopyTextureRegion(&dl, 0, 0, 0, &sl, nullptr);
        D3D12_RESOURCE_BARRIER back = Transition(s.image, D3D12_RESOURCE_STATE_COPY_DEST,
                                                 D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        list->ResourceBarrier(1, &back);
        s.pending = false;
        s.image_fresh = false;
    }
    else if (s.image_fresh)
        return src;   // nothing was ever uploaded
    // The frame as it is, then the pointer over it.
    D3D12_RESOURCE_BARRIER pre = Transition(s.target, s.target_state, D3D12_RESOURCE_STATE_COPY_DEST);
    list->ResourceBarrier(1, &pre);
    list->CopyResource(s.target, src);
    D3D12_RESOURCE_BARRIER mid[2] = {
        Transition(s.target, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        Transition(src, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)};
    list->ResourceBarrier(2, mid);
    static UINT ring = 0;
    const UINT step = h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    const UINT base = (ring++ % 8u) * 3u;
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = s.heap->GetCPUDescriptorHandleForHeapStart();
    cpu.ptr += static_cast<SIZE_T>(base) * step;
    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    sd.Format = desc.Format;
    h.dev->CreateShaderResourceView(src, &sd, cpu); cpu.ptr += step;
    sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    h.dev->CreateShaderResourceView(s.image, &sd, cpu); cpu.ptr += step;
    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = desc.Format;
    ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    h.dev->CreateUnorderedAccessView(s.target, nullptr, &ud, cpu);
    const bool linear = desc.Format == DXGI_FORMAT_R16G16B16A16_FLOAT;
    struct { int origin[2]; UINT size[2], frame[2]; float scale; UINT flags; } cb = {
        {ox, oy}, {sw, sh}, {static_cast<UINT>(desc.Width), desc.Height}, scale,
        linear ? 1u : 0u};
    static_assert(sizeof(cb) == 32, "CursorCB is 8 constants");
    ID3D12DescriptorHeap *heaps[] = {s.heap.get()};
    list->SetDescriptorHeaps(1, heaps);
    list->SetComputeRootSignature(s.root.get());
    list->SetPipelineState(s.pso.get());
    list->SetComputeRoot32BitConstants(0, 8, &cb, 0);
    D3D12_GPU_DESCRIPTOR_HANDLE gpu = s.heap->GetGPUDescriptorHandleForHeapStart();
    gpu.ptr += static_cast<UINT64>(base) * step;
    list->SetComputeRootDescriptorTable(1, gpu);
    list->Dispatch((sw + 7) / 8, (sh + 7) / 8, 1);
    D3D12_RESOURCE_BARRIER post[2] = {
        Transition(s.target, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE),
        Transition(src, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_SOURCE)};
    list->ResourceBarrier(2, post);
    s.target_state = D3D12_RESOURCE_STATE_COPY_SOURCE;
    return s.target;
}
