/**/
#ifndef __USER_CONFIG_H__
#define __USER_CONFIG_H__

#define CI_CHIP_TYPE 1302

#define MIC_DIFF_SINGLE             0 //1，单端。0，差分（通用模块都是差分模式，省成本的模块为单端（MICN_L 接GND）时，需要配置为SINGLE）

#define AUDIO_PLAYER_ENABLE         1

#if AUDIO_PLAYER_ENABLE
#define USE_PROMPT_DECODER 1          //播放器是否支持prompt解码器
#define USE_MP3_DECODER 0             //为1时加入mp3解码器
#define AUDIO_PLAY_SUPPT_MP3_PROMPT 0 //播放器默认开启mp3播报音

#define AUDIO_PLAY_BLOCK_CONT 4 //播放器底层缓冲区个数
#endif

#define USE_ALC_AUTO_SWITCH_MODULE 1          //使用动态alc模块:1开启，0关闭
#define USE_DENOISE_MODULE         0         //使用降噪模块:1开启，0关闭
#define USE_DOA_MODULE             0        //使用声源定位模块：1开启，0关闭
#define USE_DEREVERB_MODULE        0       //使用降混响模块：1开启，0关闭
#define USE_BEAMFORMING_MODULE     0       //使用双麦语音增强模块:1开启，0关闭
#define USE_AEC_MODULE             0      //使用回声消除模块:1开启，0关闭

#if USE_AEC_MODULE
#define PAUSE_VOICE_IN_WITH_PLAYING  0//开启aec时关闭
#endif

/* v20: keep the DAC powered and only gate HPOUT between clips.
   With the SDK default (0) icodec_stop() calls inner_codec_dac_disable(),
   which ASSIGNS reg2a = (1<<4) and so wipes the analog power/bias bits that
   inner_codec_power_up() and inner_codec_up_ibas_dac() set during icodec_init.
   icodec_start() then calls inner_codec_dac_enable(false), which skips
   up_ibas_dac(), so the bias is never restored: reg2a/reg2b read back as
   enabled and unmuted (measured 0xE0 / 0xF7) while the analog output stays
   dead. Value 1 is the vendor's own supported setting - cwsl_sample uses it,
   and so does this project whenever AEC is enabled. */
#define IF_JUST_CLOSE_HPOUT_WHILE_NO_PLAY   1


#if USE_BEAMFORMING_MODULE || USE_AEC_MODULE || USE_DOA_MODULE ||USE_DEREVERB_MODULE
#define HOST_CODEC_CHA_NUM  2
#endif


#define CONFIG_CI_LOG_UART 0

#define MSG_COM_USE_UART_EN 0
#define UART_PROTOCOL_NUMBER (HAL_UART1_BASE)
#define UART_PROTOCOL_BAUDRATE (UART_BaudRate115200)
#define UART_PROTOCOL_VER 2 //串口协议版本号，1：一代协议，2：二代协议，255：平台生成协议(只有发送没有接收)


#define USE_EXTERNAL_CRYSTAL_OSC             0

#if (USE_EXTERNAL_CRYSTAL_OSC == 0)
#define UART_BAUDRATE_CALIBRATE         0           // 是否使能波特率校准功能
#define BAUDRATE_SYNC_PERIOD            300000      // 波特率同步周期，单位毫秒
#define BAUDRATE_FAST_SYNC_PERIOD       5000         // 一次波特率校准识别后，下一次同步周期，单位毫秒
#define BAUD_CALIBRATE_MAX_WAIT_TIME    400          // 等待反馈包的超时时间，单位毫秒


#endif




#define BOARD_PORT_FILE "CI-D02GS01J.c"
#define UART0_PAD_OPENDRAIN_MODE_EN 1
#define UART1_PAD_OPENDRAIN_MODE_EN 1
#define COMMAND_LINE_CONSOLE_EN 0
#endif /* _USER_CONFIG_H_ */

#define WE_HOST_PROMPT_ONLY 1
/* v24: enable the SDK I2C slave (address 0x64) so the robot can reach the
   module over its 4-pin header instead of USB.
   MSG_USE_I2C_EN alone is not enough: it compiles i2c_communicate_init() in,
   but that calls pad_config_for_i2c(), which this board only defines under
   USE_IIC_PAD. Without it the weak stub in board_default.c links instead and
   prints "no function", so IIC0 is initialised with no pins on it.
   On CI-D02GS01J the pads are PB7 (IIC0_SDA) and PC0 (IIC0_SCL). The power
   amplifier is on PC4, so this does not touch it. */
#define USE_IIC_PAD 1
#define MSG_USE_I2C_EN 1
/* v19: keep the host UART prompt feature, but let the SDK play its own
   ASR responses again so the factory playback path can be compared. */
#define WE_SUPPRESS_SDK_PROMPT 1
/* v25: back to the SDK default. Auto-detection reads PC4 with the internal
   pull DISABLED, so a board without a firm external pull latches whichever
   level the floating pin happened to show at power-up. Guess high and
   power_amplifier_on() drives PC4 high, which is SD asserted on a TC8002D -
   the amplifier shuts down while every digital diagnostic still looks perfect.
   That is exactly the intermittent silence we chased. 0 = fixed active low,
   which the SDK header documents as this board family's default. */
#define GS0XJ_BOARD_PA_AUTO 0

#define WE_PCM_PROMPT_INPUT_BYTES 1152U

/* v15 probe: play prompts through codec 0 = IIS0 on PA2..PA6 pads
   (the on-board speaker amp is a digital I2S part per Hiwonder docs). */
#define PLAY_CODEC_ID 1
