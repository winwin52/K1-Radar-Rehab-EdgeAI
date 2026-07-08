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
 
#ifndef __R3_DATABOX_2D_DSP_H
#define __R3_DATABOX_2D_DSP_H

#include "mmw_type.h"

typedef struct {
    uint32_t range_start;		//Start idx of range bin to be save
	uint32_t range_num;			//Number of range bin to be saved from range_start
	uint32_t start_intv;		//Start idx of interval to be saved
	uint32_t end_intv;			//End idx of interval to be saved(should not be last interval)
	uint32_t intv_num;			//The num of intv required
	uint8_t tx_id;				//Tx antana idx specified of range fft data
	uint8_t rx_id;				//Rx antana idx specified of range fft data
	uint8_t config_mimo_rx_num;	//The num of mimo rx required
} Databox1dFftDataConfig_t;

/*	callback fuction in 2d frame mode	*/
extern int r3_databox_2d_frame_cb(void *mmw_data, void *arg);

/*	init range fft result data that need to be reported in 2d frame mode	*/
extern int r3_databox_obtain_1d_fft_data_init(void);

/*	deinit range fft result data that need to be reported in 2d frame mode	*/
extern void r3_databox_obtain_1d_fft_data_deinit(void);

/*	obtain the range fft result point in 2d frame mode	*/
extern Complex16_RealImag *r3_databox_get_1d_fft_buffer(void);

/*	obtain the range fft result config that need to be reported in 2d frame mode	*/
extern Databox1dFftDataConfig_t r3_databox_get_1d_fft_config(void);

#endif