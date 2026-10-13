// The pinned drlibs overlay builds dr_wav in a static library. Its types and errors stay private
// to the adapter, and sink exceptions are captured rather than allowed to unwind through C.
#pragma warning(push, 0)
#include <dr_wav.h>
#pragma warning(pop)

#include "wav_codec.h"

#include "audio_preflight.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <functional>
#include <span>

namespace unpackrat::codecs
{
    namespace
    {
        // Live state shared with dr_wav's C write callback. ``sink`` is borrowed for the duration
        // of write_wav; ``written`` counts the bytes already handed to the sink so the output
        // budget bounds the whole produced stream; ``failure`` records the first sink exception so
        // it can be rethrown once control is back in C++.
        struct sink_state final
        {
            sink_state(const std::function<void(std::span<const std::byte>)> &output, const std::size_t max_bytes)
                : sink(output), budget(max_bytes)
            {
            }

            const std::function<void(std::span<const std::byte>)> &sink;
            std::size_t budget = 0;
            std::size_t written = 0;
            bool budget_exceeded = false;
            std::exception_ptr failure;
        };

        // dr_wav's stream callback. It is noexcept because it is invoked from C: it must never let
        // a C++ exception cross the library boundary, nor allocate here (constructing an audio_error
        // inside a noexcept callback could terminate). A breached budget is recorded as a plain
        // flag and a sink exception as the first exception_ptr; either way the callback reports a
        // short write and then refuses every later call, so dr_wav's continued header writes cannot
        // overwrite the first failure. write_wav surfaces the recorded cause afterwards.
        size_t write_output(void *const user, const void *const data, const size_t length) noexcept
        {
            auto &state = *static_cast<sink_state *>(user);
            if (state.budget_exceeded) {
                return 0;
            }
            if (state.failure != nullptr) {
                return 0;
            }
            if (length > state.budget - state.written) {
                state.budget_exceeded = true;
                return 0;
            }
            try {
                state.sink(std::span<const std::byte>(static_cast<const std::byte *>(data), length));
            } catch (...) {
                state.failure = std::current_exception();
                return 0;
            }
            state.written += length;
            return length;
        }

        // Owns a dr_wav writer and always runs the matching drwav_uninit, including while a
        // rethrown sink exception unwinds. The destructor's uninit is safe on a partially
        // initialized writer, and a well-formed preinit'd writer only ever needs one release.
        class writer_guard final
        {
          public:
            writer_guard() = default;
            writer_guard(const writer_guard &) = delete;
            writer_guard &operator=(const writer_guard &) = delete;

            ~writer_guard()
            {
                if (armed_) {
                    drwav_uninit(&writer_);
                }
            }

            [[nodiscard]] drwav *writer() noexcept
            {
                return &writer_;
            }
            void arm() noexcept
            {
                armed_ = true;
            }
            void disarm() noexcept
            {
                armed_ = false;
            }

          private:
            drwav writer_{};
            bool armed_ = false;
        };

    } // namespace

    void write_wav(const pcm_audio &audio, const std::function<void(std::span<const std::byte>)> &sink,
                   const audio_limits &limits)
    {
        const std::uint32_t channels = audio.channels;
        if (channels != 1 && channels != 2) {
            throw audio_error("wav: only mono and stereo PCM16 output is supported");
        }
        if (audio.sample_rate == 0) {
            throw audio_error("wav: sample rate must be non-zero");
        }
        if (audio.samples.size() % channels != 0) {
            throw audio_error("wav: sample count is not a whole number of frames");
        }
        // dr_wav derives the fmt byte rate with 32-bit arithmetic as (16 * sampleRate * channels) / 8,
        // so a high rate can wrap that product to a zero or wrong byte rate; reject it before any
        // byte reaches the sink.
        if (static_cast<std::uint64_t>(audio.sample_rate) * channels * 16 > 0xFFFFFFFFULL) {
            throw audio_error("wav: the sample rate overflows the format byte rate");
        }

        const std::size_t frames = audio.samples.size() / channels;
        if (frames > limits.max_pcm_frames) {
            throw audio_error("wav: frame count exceeds the configured budget");
        }
        // The PCM payload is the dominant part of the stream; bounding it here keeps the untrusted
        // sample count from driving an oversized write before the callback budget can react. The
        // callback still enforces the budget on the whole stream, header included.
        if (audio.samples.size() > limits.max_output_bytes / 2) {
            throw audio_error("wav: output exceeds the configured byte budget");
        }
        // The RIFF container caps the payload independently of any caller budget, so the stream is
        // rejected up front as soon as the container cannot describe it. This mirrors the exact
        // condition dr_wav's sequential writer init evaluates, so init cannot later fail on the
        // payload size.
        static_cast<void>(preflight::riff_pcm16_stream_bytes(frames, channels));

        sink_state state{sink, limits.max_output_bytes};

        drwav_data_format format{};
        format.container = drwav_container_riff;
        format.format = DR_WAVE_FORMAT_PCM;
        format.channels = channels;
        format.sampleRate = audio.sample_rate;
        format.bitsPerSample = 16;

        writer_guard guard;
        // The format (RIFF/PCM/16-bit, one or two channels, an accepted rate and payload) is fully
        // validated above, which is exactly the set of conditions dr_wav's writer init checks, so
        // initialization succeeds and every real failure comes from the sink or the budget and is
        // recorded in ``state``. The guard still arms only once the writer exists so its matching
        // uninit is paired on every path.
        static_cast<void>(
            drwav_init_write_sequential_pcm_frames(guard.writer(), &format, frames, write_output, &state, nullptr));
        guard.arm();

        if (frames != 0) {
            // A short write can only be the callback refusing the stream, so the recorded failure is
            // the authoritative result and is rethrown below instead of a synthesized error.
            static_cast<void>(drwav_write_pcm_frames(guard.writer(), frames, audio.samples.data()));
        }

        // A sink exception is surfaced first, while the guard is still armed, so the matching uninit
        // runs from the guard's destructor exactly as it would for any other unwind.
        if (state.failure) {
            std::rethrow_exception(state.failure);
        }
        // The callback records a refused write as the sticky ``budget_exceeded`` flag, and that flag
        // is the authoritative budget result. dr_wav reports a short write through its sequential
        // finalizer, but an empty stream fixes a zero-byte data target that is already satisfied, so
        // a header-only refusal would otherwise finalize as a success. Both causes are surfaced here,
        // outside the noexcept callback, where an audio_error may be constructed; a sink exception
        // was already rethrown. A sequential writer never seeks, so uninit writes no further data.
        const drwav_result finalized = drwav_uninit(guard.writer());
        guard.disarm();
        if (finalized != DRWAV_SUCCESS || state.budget_exceeded) {
            throw audio_error("wav: output exceeds the configured byte budget");
        }
    }

    void write_wav_streaming(const std::uint64_t frames, const std::uint32_t channels, const std::uint32_t sample_rate,
                             const std::function<void(std::uint64_t, std::span<std::int16_t>)> &pull,
                             const std::function<void(std::span<const std::byte>)> &sink, const audio_limits &limits)
    {
        if (channels != 1 && channels != 2) {
            throw audio_error("wav: only mono and stereo PCM16 output is supported");
        }
        if (sample_rate == 0 || static_cast<std::uint64_t>(sample_rate) * channels * 16 > 0xFFFFFFFFULL) {
            throw audio_error("wav: sample rate is invalid for PCM16 output");
        }
        if (frames > limits.max_pcm_frames ||
            preflight::riff_pcm16_stream_bytes(frames, channels) > limits.max_output_bytes) {
            throw audio_error("wav: output exceeds the configured budget");
        }

        sink_state state{sink, limits.max_output_bytes};
        drwav_data_format format{};
        format.container = drwav_container_riff;
        format.format = DR_WAVE_FORMAT_PCM;
        format.channels = channels;
        format.sampleRate = sample_rate;
        format.bitsPerSample = 16;

        writer_guard guard;
        // The same RIFF/PCM16 preflight as write_wav makes init infallible. The pinned writer
        // reports header callback failures through our sticky state, never through its init result.
        static_cast<void>(
            drwav_init_write_sequential_pcm_frames(guard.writer(), &format, frames, write_output, &state, nullptr));
        guard.arm();
        if (state.failure) {
            std::rethrow_exception(state.failure);
        }
        std::array<std::int16_t, 4096> samples{};
        std::uint64_t produced = 0;
        while (produced < frames) {
            const std::size_t count = static_cast<std::size_t>((std::min)(frames - produced, 2048ULL));
            const std::size_t sample_count = count * channels;
            pull(produced, std::span<std::int16_t>(samples.data(), sample_count));
            // The whole stream was budgeted above and this bounded, positive chunk is always
            // representable to dr_wav. Only a sink exception can refuse its exact-length write.
            static_cast<void>(drwav_write_pcm_frames(guard.writer(), count, samples.data()));
            if (state.failure) {
                std::rethrow_exception(state.failure);
            }
            produced += count;
        }
        // Every declared frame reached the sink. PCM16 needs no RIFF padding, and the sequential
        // finalizer only checks that exact data count; it cannot fail or write further bytes here.
        static_cast<void>(drwav_uninit(guard.writer()));
        guard.disarm();
    }
} // namespace unpackrat::codecs
