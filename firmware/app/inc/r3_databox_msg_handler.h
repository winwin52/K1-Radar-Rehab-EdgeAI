/**
 **************************************************************************************************
 * @brief   project config define.
 * @attention
 *
 * Copyright (C) 2025 POSSUMIC TECHNOLOGY CO., LTD. All rights reserved.
 *
 *  Redistribution and use in source and binary forms, with or without
 *  modification, are permitted provided that the following conditions
 *  are met:
 *    1. Redistributions of source code must retain the above copyright
 *       notice, this list of conditions and the following disclaimer.
 *    2. Redistributions in binary form must reproduce the above copyright
 *       notice, this list of conditions and the following disclaimer in the
 *       documentation and/or other materials provided with the
 *       distribution.
 *    3. Neither the name of POSSUMIC TECHNOLOGY CO., LTD. nor the names of
 *       its contributors may be used to endorse or promote products derived
 *       from this software without specific prior written permission.
 *
 *  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 *  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 *  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 *  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 *  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 *  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 *  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 *  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 *  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 *  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 *  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *
 **************************************************************************************************
 */

#ifndef __R3_DATABOX_MSG_HANDLER_H__
#define __R3_DATABOX_MSG_HANDLER_H__


#ifdef __cplusplus
extern "C" {
#endif

#include <common.h>

#define HIF_MSG_ID_MOTION_SENSOR_PARA_CFG       0x60
#define HIF_MSG_ID_MOTION_SENSOR_INFO_GET       0x61

/* Skip frame for r3_databox application */
#define R3_DATABOX_SKIP_FRAME_NUM				2

typedef struct {
    uint8_t type;
    uint8_t len;
    uint8_t value[1];
} strMsgTlv;

// SECTION CONFIG

// FRAME CONFIG
#define TLV_R3_DATA_BOX_CONFIG_MIMO_MODE			0x10
#define TLV_R3_DATA_BOX_CONFIG_FRAME_TYPE			0x11
#define TLV_R3_DATA_BOX_CONFIG_START_FREQ			0x12
#define TLV_R3_DATA_BOX_CONFIG_TRIGGER_RANGE		0x13
#define TLV_R3_DATA_BOX_CONFIG_RANGE_RESOLUTION		0x14
#define TLV_R3_DATA_BOX_CONFIG_MAX_VELOCITY			0x15
#define TLV_R3_DATA_BOX_CONFIG_VEL_RESOLUTION		0x16
#define TLV_RE_DATA_BOX_CONFIG_FRAME_PERIOD			0x17

/* BB CONFIG */

/* POINT CLOUD CONFIG */

/* TRAJ CONFIG */

/* UPLOAD CONFIG */

int r3_databox_msg_init(void);

typedef struct{
	uint8_t mimo_mode;				/* use ant mode, see MMW_MIMO_xx in mmw_ctrl.h */
	uint8_t frame_type;				/* type 0: use 1d frame, 1: use 2d frame */
	uint32_t start_freq_mhz;		/* radar work srart freq (1 LSB = 1 mhz) */
	uint32_t max_trigger_range_mm;	/* max detection range (1 LSB = 1 mm) */
	uint32_t range_resolution_mm;	/* range resolution (1 LSB = 1 mm) */
	uint32_t max_range_rate_mm;		/* maximum unambiguous velocity (1 LSB = 1 mm/s) */
	uint32_t vel_resolution_mm;		/* velocity resolution (1 LSB = 1 mm/s) */
	uint32_t frame_period_ms;		/* frame period (1LSB = 1ms) */
} DataBoxFrameConfig_t;

typedef struct{
	 DataBoxFrameConfig_t data_box_frame_config;
} DataBoxConfig_t;

/* config radar work para from g_databox_global_config */
extern int r3_databox_startup_config();
/* obtain the skip frame status,it has been completed when returning to 0, skip R3_DATABOX_SKIP_FRAME_NUM frame after call mmw_ctrl_start() */
uint8_t r3_databox_obtain_skip_frame_status(void);

/* Count skipped frames, call this function when r3_databox_get_g_call_back_entry() not reached condition */
void r3_databox_update_skip_frame_status(void);

/* reset entery flag in this function */
void r3_databox_reset_skip_frame_status(void);
#ifdef __cplusplus
}
#endif
#endif  //__R3_DATABOX_MSG_HANDLER_H__

