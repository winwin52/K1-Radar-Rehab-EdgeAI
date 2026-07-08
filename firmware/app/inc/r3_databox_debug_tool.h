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
#ifndef __R3_DATABOX_DEBUG_TOOL_H
#define __R3_DATABOX_DEBUG_TOOL_H

#include "mmw_ctrl.h"
#include "mmw_point_cloud_psic_lib.h"

/* print the radar work para */
extern void dbg_printk_config(void);

/* upload point cloud process result */
extern void r3_databox_point_cloud_upload(const PointCloudBuffer_t *ptr_3d_pc, uint16_t pc_len);


#if (CONFIG_MMW_MICRO_POINT_CLOUD)
/* upload micro point cloud process result */
extern void mmw_micro_point_cloud_upload(PointCloud3D *ptr_3d_mpc, uint16_t mpc_len);
#endif

/* upload zeros dop bin data (in 1d frame mode,upload 1dfft data) */
extern void r3_databox_upload_zeros_dop_bin_data(void);

/* upload fft auto gain */
extern void r3_databox_upload_gain_factor_process(void);

/* upload range fft result in 2d frame mode */
extern void r3_databox_upload_1d_fft_data(void);

/* reset s_databox_gain_factor_frame_idx to 0 */
extern void r3_databox_reset_gain_factor_frame_idx(void);
#endif